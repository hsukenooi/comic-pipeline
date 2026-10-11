"""Tests for ebay-shipped --auto-submit (BUI-1238).

The XML mirrors a live GetOrders (OrderRole=Buyer) response captured 2026-10-11:
the shipping location is Item/Location as "City, State Name" with NO country
element, tracking sits in Transaction/ShippingDetails/ShipmentTrackingDetails,
and the order-level CancelStatus is NotApplicable. Identifiers are invented.
"""

import json

import pytest
import requests

import ebay_user_token as eut
import shipped_orders
import shipped_submit as ss

NSDECL = 'xmlns="urn:ebay:apis:eBLBaseComponents"'


def txn(item_id, title="Some Title", loc="Lake Forest, Illinois", carrier="USPS",
        number="9400111899223197428490"):
    tracking = (
        f"<ShipmentTrackingDetails><ShippingCarrierUsed>{carrier}</ShippingCarrierUsed>"
        f"<ShipmentTrackingNumber>{number}</ShipmentTrackingNumber></ShipmentTrackingDetails>"
        if number else ""
    )
    return (
        f"<Transaction><ShippingDetails>{tracking}</ShippingDetails>"
        f"<Item><ItemID>{item_id}</ItemID><Location>{loc}</Location><Site>US</Site>"
        f"<Title>{title}</Title></Item><ShippedTime>2026-10-05T14:16:05.000Z</ShippedTime>"
        "</Transaction>"
    )


def order(order_id, txns, seller="seller_a", cancel="NotApplicable"):
    return (
        f"<Order><OrderID>{order_id}</OrderID><OrderStatus>Completed</OrderStatus>"
        f"<TransactionArray>{''.join(txns)}</TransactionArray>"
        f"<SellerUserID>{seller}</SellerUserID><CancelStatus>{cancel}</CancelStatus></Order>"
    )


def response(orders, more=False, ack="Success", omit_array=False):
    arr = "" if omit_array else f"<OrderArray>{''.join(orders)}</OrderArray>"
    return (
        f'<?xml version="1.0"?><GetOrdersResponse {NSDECL}><Ack>{ack}</Ack>'
        f"<HasMoreOrders>{'true' if more else 'false'}</HasMoreOrders>{arr}</GetOrdersResponse>"
    ).encode()


def rows_from(*orders):
    import xml.etree.ElementTree as ET
    return ss.parse_orders(ET.fromstring(response(list(orders))))


# ─── grouping ─────────────────────────────────────────────────────────────────

def test_one_group_per_tracking_with_joined_titles():
    rows = rows_from(
        order("O1", [txn("1", "ebay one"), txn("2", "ebay two")]),
        order("O2", [txn("3", "ebay three", number="1Z999AA10123456784", carrier="UPS")]),
    )
    groups, skipped = ss.group_by_tracking(rows, {"1": "ASM  #1", "2": "ASM #2"})
    assert skipped == []
    by = {g["tracking"]: g for g in groups}
    assert len(groups) == 2
    g = by["9400111899223197428490"]
    assert g["titles"] == ["ASM #1", "ASM #2"]  # gixen titles, whitespace collapsed
    assert g["title_misses"] == []
    assert ss.product_name(g["titles"]) == "ASM #1; ASM #2"


def test_title_lookup_miss_falls_back_to_ebay_title_and_is_flagged():
    rows = rows_from(order("O1", [txn("1", "ebay one"), txn("2", "ebay two")]))
    (g,), _ = ss.group_by_tracking(rows, {"1": "Gixen One"})
    assert g["titles"] == ["Gixen One", "ebay two"]
    assert g["title_misses"] == ["2"]


def test_duplicate_item_rows_are_not_double_counted():
    rows = rows_from(order("O1", [txn("1"), txn("1")]))
    (g,), _ = ss.group_by_tracking(rows, {})
    assert g["item_ids"] == ["1"]


def test_missing_tracking_and_cancelled_are_skipped_not_submitted():
    rows = rows_from(
        order("O1", [txn("1", number=None)]),
        order("O2", [txn("2")], cancel="CancelComplete"),
    )
    groups, skipped = ss.group_by_tracking(rows, {})
    assert groups == []
    assert sorted(s["reason"] for s in skipped) == ["cancelled", "no_tracking"]


def test_product_name_is_capped():
    assert len(ss.product_name(["x" * 100, "y" * 100, "z" * 100])) == ss.PRODUCT_NAME_MAX


# ─── hold rules ───────────────────────────────────────────────────────────────

def _classify(loc="Lake Forest, Illinois", carrier="USPS", submitted=frozenset()):
    rows = rows_from(order("O1", [txn("1", loc=loc, carrier=carrier)]))
    (g,), _ = ss.group_by_tracking(rows, {})
    return ss.classify(g, submitted)


def test_us_usps_submits():
    d = _classify()
    assert (d["action"], d["ezship_carrier"]) == ("submit", "USPS")


@pytest.mark.parametrize("loc", ["London, United Kingdom", "Shenzhen, China", "", "Toronto",
                                 "Springfield, IL", "Hong Kong, Hong Kong"])
def test_non_us_or_unreadable_location_holds(loc):
    d = _classify(loc=loc)
    assert d["action"] == "hold" and "non-US" in d["reason"]


@pytest.mark.parametrize("carrier", ["Royal Mail", "Canada Post", "", "Yanwen", "USPSx"])
def test_unsupported_carrier_holds(carrier):
    d = _classify(carrier=carrier)
    assert d["action"] == "hold" and "unsupported carrier" in d["reason"]


@pytest.mark.parametrize("raw,mapped", [("USPS", "USPS"), ("FedEx", "FedEx"), ("FEDEX", "FedEx"),
                                        ("UPS", "UPS"), ("DHL", "DHL"), ("OnTrac", "Ontrac"),
                                        ("Amazon", "Amazon"), ("Other", "Other")])
def test_all_seven_ezship_carriers_map(raw, mapped):
    assert ss.normalize_carrier(raw) == mapped


def test_explicit_non_us_country_holds_even_with_us_looking_location():
    assert ss.ships_from_us("Austin, Texas", "CN") is False
    assert ss.ships_from_us("Austin, Texas", "US") is True


def test_ledger_dedupe_beats_hold_and_is_case_insensitive():
    d = _classify(submitted={"9400111899223197428490"})
    assert d["action"] == "skip"
    d = _classify(carrier="Royal Mail", submitted={"9400111899223197428490"})
    assert d["action"] == "skip"


# ─── GetOrders fetching ───────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status


def test_pagination_follows_has_more(monkeypatch):
    pages = [response([order("O1", [txn("1")])], more=True), response([order("O2", [txn("2")])])]
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(pages.pop(0)))
    assert [r["item_id"] for r in ss.fetch_transactions("tok")] == ["1", "2"]


def test_missing_order_array_is_an_error_not_no_orders(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(response([], omit_array=True)))
    with pytest.raises(ss.SubmitError, match="no OrderArray"):
        ss.fetch_transactions("tok")


def test_ack_failure_and_http_error_raise(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(response([], ack="Failure")))
    with pytest.raises(ss.SubmitError, match="Ack=Failure"):
        ss.fetch_transactions("tok")
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(b"", 503))
    with pytest.raises(ss.SubmitError, match="503"):
        ss.fetch_transactions("tok")


# ─── orchestration ────────────────────────────────────────────────────────────

class Env:
    """Wires run() to fakes and records every side effect."""

    def __init__(self, monkeypatch, tmp_path, rows, ledger=None, ezship=None):
        self.ledger = {k.upper() for k in (ledger or [])}
        self.ezship_calls, self.notes, self.pings = [], [], []
        self.ezship = ezship or self.ok_ezship
        monkeypatch.setattr(eut, "get_user_access_token", lambda: "tok")
        monkeypatch.setattr(ss, "fetch_transactions", lambda token, **k: rows)
        monkeypatch.setattr(ss, "load_gixen_titles", lambda: ({}, None))
        monkeypatch.setattr(ss, "run_ezship", self._run)
        monkeypatch.setattr(ss, "notify", self.notes.append)
        monkeypatch.setattr(ss, "ping_heartbeat", lambda url, ok, d: self.pings.append((ok, d)))
        self.holds = tmp_path / "holds.json"
        self.inflight = tmp_path / "auto-submit-inflight.json"

    def ok_ezship(self, group):
        self.ledger.add(group["tracking"].upper())  # ezship records on success
        return 0, "ok"

    def _run(self, group):
        self.ezship_calls.append(group["tracking"])
        return self.ezship(group)

    def go(self, dry_run=False):
        return ss.run(ledger_path="unused", load_ledger=lambda p: set(self.ledger),
                      dry_run=dry_run, holds_path=self.holds, server_url="http://x")


def two_orders():
    return rows_from(
        order("O1", [txn("1")]),
        order("O2", [txn("2", number="1Z999AA10123456784", carrier="UPS")]),
    )


def test_dry_run_never_calls_ezship_or_writes_state(monkeypatch, tmp_path):
    rows = rows_from(order("O1", [txn("1")]), order("O2", [txn("2", loc="London, United Kingdom",
                                                                number="RM123456789GB")]))
    env = Env(monkeypatch, tmp_path, rows)
    assert env.go(dry_run=True) == 0
    assert env.ezship_calls == [] and env.notes == [] and env.pings == []
    assert not env.holds.exists()


def test_submit_mode_submits_new_and_pings_heartbeat(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, two_orders())
    assert env.go() == 0
    assert sorted(env.ezship_calls) == ["1Z999AA10123456784", "9400111899223197428490"]
    assert env.pings == [(True, "submitted=2 held=0 already=0")]


def test_rerun_does_not_double_submit(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, two_orders())
    assert env.go() == 0
    env.ezship_calls.clear()
    assert env.go() == 0
    assert env.ezship_calls == []


def test_ledgered_tracking_is_never_resubmitted(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, two_orders(), ledger=["9400111899223197428490"])
    env.go()
    assert env.ezship_calls == ["1Z999AA10123456784"]


def test_hold_notifies_once_and_does_not_submit(monkeypatch, tmp_path):
    rows = rows_from(order("O1", [txn("1", carrier="Royal Mail", number="RM123456789GB")]))
    env = Env(monkeypatch, tmp_path, rows)
    assert env.go() == 0
    assert env.ezship_calls == [] and len(env.notes) == 1 and "RM123456789GB" in env.notes[0]
    assert env.pings and env.pings[0][0] is True  # a hold is a correct outcome
    env.go()
    assert len(env.notes) == 1  # not re-notified on the next run


def test_exit_3_stops_batch_notifies_and_withholds_heartbeat(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, two_orders(), ezship=lambda g: (3, "Session expired"))
    assert env.go() == 3
    assert len(env.ezship_calls) == 1  # the second order was never attempted
    assert any("session expired" in n.lower() for n in env.notes)
    assert [ok for ok, _ in env.pings] == [False]


def test_rejection_continues_batch_exits_4_and_pings_failure(monkeypatch, tmp_path):
    def ezship(group):
        if group["tracking"].startswith("1Z"):
            return 1, "Error: rejected"
        return Env.ok_ezship(env, group)
    env = Env(monkeypatch, tmp_path, two_orders(), ezship=ezship)
    assert env.go() == 4
    assert len(env.ezship_calls) == 2
    assert [ok for ok, _ in env.pings] == [False]


def test_exit_0_without_ledger_entry_stops_batch(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, two_orders(), ezship=lambda g: (0, "ok"))
    assert env.go() == 1
    assert len(env.ezship_calls) == 1
    assert [ok for ok, _ in env.pings] == [False]


def test_timeout_stops_batch_as_unconfirmed(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, two_orders(), ezship=lambda g: (None, "timed out"))
    assert env.go() == 1
    assert len(env.ezship_calls) == 1


def test_launch_failure_stops_batch_instead_of_failing_every_order(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, two_orders(), ezship=lambda g: (-1, "could not launch ezship"))
    assert env.go() == 1
    assert len(env.ezship_calls) == 1


def test_ebay_failure_is_a_hard_failure_not_no_orders(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, [])
    monkeypatch.setattr(ss, "fetch_transactions",
                        lambda t, **k: (_ for _ in ()).throw(ss.SubmitError("boom")))
    assert env.go() == 1
    assert [ok for ok, _ in env.pings] == [False]


def test_token_failure_is_hard_failure(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, [])
    monkeypatch.setattr(eut, "get_user_access_token",
                        lambda: (_ for _ in ()).throw(eut.UserTokenError("expired")))
    assert env.go() == 1 and env.ezship_calls == []


def test_unreadable_ledger_is_hard_failure(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, two_orders())
    def bad(p):
        raise shipped_orders.ShippedOrdersError("corrupt")
    assert ss.run(ledger_path="x", load_ledger=bad, dry_run=False, holds_path=env.holds) == 1
    assert env.ezship_calls == []


def test_ezship_command_omits_declared_value_and_uses_books():
    rows = rows_from(order("O1", [txn("1", "T")]))
    (g,), _ = ss.group_by_tracking(rows, {})
    cmd = ss.ezship_command(ss.classify(g, set()))
    assert "-d" not in cmd and "--declared-value" not in cmd
    assert cmd[cmd.index("-c") + 1] == "USPS" and cmd[cmd.index("--category") + 1] == "Books"
    assert cmd[cmd.index("-t") + 1] == "9400111899223197428490"


def test_gixen_failure_degrades_to_ebay_titles(monkeypatch):
    def boom(*a, **k):
        raise OSError("no gixen")
    monkeypatch.setattr(ss.subprocess, "run", boom)
    titles, warning = ss.load_gixen_titles()
    assert titles == {} and "gixen list failed" in warning


# ─── CLI wiring ───────────────────────────────────────────────────────────────

def test_cli_routes_auto_submit_and_rejects_bare_dry_run(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(ss, "run", lambda **k: seen.update(k) or 0)
    assert shipped_orders.main(["--auto-submit", "--dry-run"]) == 0
    assert seen["dry_run"] is True
    with pytest.raises(SystemExit):
        shipped_orders.main(["--dry-run"])


def test_heartbeat_job_is_registered():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[3] / "docs" / "reference"
           / "job-heartbeat-contract.md").read_text()
    assert f"`{ss.HEARTBEAT_JOB}`" in doc


# ─── in-flight quarantine (review blockers) ───────────────────────────────────

ONE = "9400111899223197428490"


def one_order():
    return rows_from(order("O1", [txn("1")]))


def test_timeout_quarantines_and_rerun_holds(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, one_order(), ezship=lambda g: (None, "timed out"))
    assert env.go() == 1
    assert ONE in json.loads(env.inflight.read_text())
    env.ezship_calls.clear()
    env.ezship = lambda g: Env.ok_ezship(env, g)
    env.go()
    assert env.ezship_calls == []  # held, never resubmitted
    assert any("ebay-shipped --clear-inflight" in n and ONE in n for n in env.notes)


def test_exit_0_without_ledger_quarantines(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, one_order(), ezship=lambda g: (0, "ok"))
    assert env.go() == 1
    env.ezship_calls.clear()
    env.go()
    assert env.ezship_calls == []


def test_exit_1_is_ambiguous_and_quarantined(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, one_order(), ezship=lambda g: (1, "Network error"))
    assert env.go() == 4
    env.ezship_calls.clear()
    env.ezship = lambda g: Env.ok_ezship(env, g)
    assert env.go() == 0  # a hold is a correct outcome
    assert env.ezship_calls == []


def test_clearing_marker_releases_the_hold(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, one_order(), ezship=lambda g: (1, "Network error"))
    env.go()
    assert ss.clear_inflight(env.inflight, ONE.lower()) is True
    env.ezship = lambda g: Env.ok_ezship(env, g)
    env.go()
    assert env.ezship_calls[-1] == ONE


def test_confirmed_submit_clears_marker_and_marker_exists_before_call(monkeypatch, tmp_path):
    seen = {}

    def ezship(group):
        seen["during"] = json.loads(env.inflight.read_text())
        return Env.ok_ezship(env, group)
    env = Env(monkeypatch, tmp_path, one_order(), ezship=ezship)
    assert env.go() == 0
    assert ONE in seen["during"]
    assert json.loads(env.inflight.read_text()) == {}


def test_exit_3_and_launch_failure_clear_marker(monkeypatch, tmp_path):
    for result in ((3, "Session expired"), (-1, "could not launch")):
        env = Env(monkeypatch, tmp_path, one_order(), ezship=lambda g, r=result: r)
        env.go()
        assert json.loads(env.inflight.read_text()) == {}


def test_crash_during_call_leaves_marker(monkeypatch, tmp_path):
    def boom(group):
        raise KeyboardInterrupt
    env = Env(monkeypatch, tmp_path, one_order(), ezship=boom)
    with pytest.raises(KeyboardInterrupt):
        env.go()
    env.ezship = lambda g: Env.ok_ezship(env, g)
    env.ezship_calls.clear()
    env.go()
    assert env.ezship_calls == []


def test_corrupt_marker_fails_closed(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, one_order())
    env.inflight.write_text("{not json")
    assert env.go() == 1
    assert env.ezship_calls == []


def test_unwritable_marker_blocks_submit(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, one_order())

    def bad(*a, **k):
        raise ss.InflightError("disk full")
    monkeypatch.setattr(ss, "save_inflight", bad)
    assert env.go() == 1
    assert env.ezship_calls == []


def test_dry_run_reports_quarantine_and_writes_no_marker(monkeypatch, tmp_path, capsys):
    env = Env(monkeypatch, tmp_path, one_order())
    assert env.go(dry_run=True) == 0
    assert not env.inflight.exists()
    env.inflight.write_text(json.dumps({ONE: {"since": "2026-10-01T00:00:00Z", "state": "x"}}))
    capsys.readouterr()
    assert env.go(dry_run=True) == 0
    assert "[HOLD  ]" in capsys.readouterr().out and env.ezship_calls == []


def test_concurrent_run_does_nothing(monkeypatch, tmp_path):
    import fcntl
    env = Env(monkeypatch, tmp_path, one_order())
    with open(tmp_path / "auto-submit-inflight.json.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        assert env.go() == 0
    assert env.ezship_calls == []


def test_item_with_two_tracking_numbers_is_held():
    t = ("<Transaction><ShippingDetails>"
         "<ShipmentTrackingDetails><ShippingCarrierUsed>USPS</ShippingCarrierUsed>"
         "<ShipmentTrackingNumber>AAA111</ShipmentTrackingNumber></ShipmentTrackingDetails>"
         "<ShipmentTrackingDetails><ShippingCarrierUsed>USPS</ShippingCarrierUsed>"
         "<ShipmentTrackingNumber>BBB222</ShipmentTrackingNumber></ShipmentTrackingDetails>"
         "</ShippingDetails><Item><ItemID>9</ItemID><Location>Lake Forest, Illinois</Location>"
         "<Title>T</Title></Item></Transaction>")
    groups, _ = ss.group_by_tracking(rows_from(order("O1", [t])), {})
    decisions = [ss.classify(g, set()) for g in groups]
    assert len(decisions) == 2 and all(d["action"] == "hold" for d in decisions)
