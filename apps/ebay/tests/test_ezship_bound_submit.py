"""Tests for ezship-bound --submit and full-list category matching (BUI-1425)."""

import base64
import copy
import json
from pathlib import Path

import pytest

import ezship_bound as eb
import shipped_orders as so
import shipped_submit as ss

FIX = Path(__file__).parent / "fixtures" / "ezship_bound"
HART_TRACKING = "92001902673388000127757663"
HART_ITEM = "RPL-HC-15: Dual Lemo 2-pin Cable for Focal Utopia"


def load(name):
    return json.loads((FIX / name).read_text())


def with_item(message, new_name):
    """The Hart order with its one item renamed (total stays $109.00)."""
    msg = copy.deepcopy(message)

    def walk(part):
        data = (part.get("body") or {}).get("data")
        if (part.get("mimeType") or "").lower() == "text/plain" and data:
            text = base64.urlsafe_b64decode(data).decode().replace(HART_ITEM, new_name)
            part["body"]["data"] = base64.urlsafe_b64encode(text.encode()).decode()
        for child in part.get("parts") or []:
            walk(child)

    walk(msg["payload"])
    return msg


class Harness:
    """Runs `ezship-bound --submit` with every outside call faked."""

    def __init__(self, monkeypatch, tmp_path, orders, codes=None):
        self.ledger = tmp_path / "submitted-orders.json"
        self.inflight = tmp_path / "auto-submit-inflight.json"
        self.attention = tmp_path / "attention.json"
        self.codes = list(codes or [])
        self.calls, self.notes, self.pings = [], [], []
        shipping = load("hart_shipping.json")

        def fetch(query, max_results, max_workers=8):
            if "12826" in query:
                return so.Fetched(list(orders), False, len(orders))
            if "75151" in query:
                return so.Fetched([shipping], False, 1)
            return so.Fetched([], False, 0)

        monkeypatch.setattr(eb, "fetch_messages", fetch)
        monkeypatch.setattr(eb, "load_ezship_trackings", lambda: set())
        monkeypatch.setattr(eb, "load_live_categories", eb.load_categories)
        monkeypatch.setattr(eb, "run_submit_command", self._run)
        monkeypatch.setattr(ss, "notify", self.notes.append)
        monkeypatch.setattr(eb, "ping_submit_heartbeat",
                            lambda url, ok, detail: self.pings.append((ok, detail)))

    def _run(self, fields):
        self.calls.append(fields)
        code = self.codes.pop(0) if self.codes else 0
        if code == 0:
            data = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
            data[fields["tracking"]] = {"submittedAt": "2026-11-17T00:00:00Z"}
            self.ledger.write_text(json.dumps(data))
        return code, "done"

    def main(self):
        orig = eb.run_submit

        def run_submit(a, server_url):
            return orig(a, "http://comics", inflight_path=self.inflight,
                        attention_path=self.attention)

        eb.run_submit = run_submit
        try:
            return eb.main(["--submit", "--ledger", str(self.ledger)])
        finally:
            eb.run_submit = orig


# ─── Submit ───────────────────────────────────────────────────────────────────

def test_submits_a_tshirt_order_with_its_own_minimum(monkeypatch, tmp_path, capsys):
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")])
    assert h.main() == 0
    [fields] = h.calls
    assert (fields["category"], fields["category_id"]) == ("Tops & Tee", "869")
    assert fields["declared_min_cents"] == 500
    assert fields["declared_value_cents"] == 5450       # 50% of $109.00
    assert fields["tracking"] == HART_TRACKING
    out = capsys.readouterr().out
    assert f"submitted {HART_TRACKING}" in out and "Tops & Tee (869)" in out
    assert "ezship-bound: submitted=1 attention=0 new_attention=0" in out
    assert h.pings == [(True, "submitted=1 attention=0")]
    assert json.loads(h.inflight.read_text()) == {}


def test_submit_command_carries_every_field(monkeypatch):
    monkeypatch.delenv("EZSHIP_CMD", raising=False)
    cmd = eb.submit_command({
        "tracking": "T1", "carrier": "USPS", "declared_value_cents": 5450,
        "product": "Logo T-shirt", "category": "Tops & Tee", "category_id": "869",
        "declared_min_cents": 500, "remark": "Hart Audio Cables 75151"})
    assert cmd[cmd.index("new"):] == ["new", "-t", "T1", "-c", "USPS", "-d", "5450", "-p", "Logo T-shirt",
                       "--category", "Tops & Tee", "--category-id", "869",
                       "--declared-min", "500", "-r", "Hart Audio Cables 75151"]


def test_unmatched_item_is_held_and_notified_once(monkeypatch, tmp_path, capsys):
    order = with_item(load("hart_order.json"), "Mystery Gadget")
    h = Harness(monkeypatch, tmp_path, [order])
    assert h.main() == 0
    assert h.calls == []
    assert len(h.notes) == 1 and "no_category" in h.notes[0]
    assert "new_attention=1" in capsys.readouterr().out
    assert h.pings and h.pings[-1][0] is True          # a hold is a correct outcome
    assert h.main() == 0
    assert len(h.notes) == 1                            # not repeated
    assert "attention=1 new_attention=0" in capsys.readouterr().out


def test_expired_cookie_stops_and_clears_the_marker(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")],
                codes=[3])
    assert h.main() == 3
    assert json.loads(h.inflight.read_text()) == {}
    assert h.pings[-1][0] is False
    assert any("set-cookie" in n for n in h.notes)


def test_rejected_order_is_quarantined_and_held_next_run(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")],
                codes=[1])
    assert h.main() == 4
    assert "failed exit=1" in json.loads(h.inflight.read_text())[HART_TRACKING]["state"]
    assert h.main() == 0
    assert len(h.calls) == 1                            # never retried
    assert any("QUARANTINED" in n for n in h.notes)


def test_exit_zero_without_ledger_entry_is_unconfirmed(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")])
    monkeypatch.setattr(eb, "run_submit_command",
                        lambda fields: (h.calls.append(fields), (0, "ok"))[1])
    assert h.main() == 1
    assert "unconfirmed" in json.loads(h.inflight.read_text())[HART_TRACKING]["state"]


def test_session_expired_on_the_order_list_exits_3(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [load("hart_order.json")])

    def expired():
        raise eb.EzshipSessionExpired("`ezship orders` failed (exit 3)")

    monkeypatch.setattr(eb, "load_ezship_trackings", expired)
    assert h.main() == 3
    assert h.calls == [] and h.pings[-1][0] is False


def test_truncated_search_submits_nothing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")])
    monkeypatch.setattr(eb, "fetch_messages",
                        lambda q, m, w=8: so.Fetched([], True, 0))
    assert h.main() == 2
    assert h.calls == [] and h.pings[-1][0] is False


def test_submit_rejects_json(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        eb.main(["--submit", "--json"])


def test_live_category_lookup_failure_exits_1(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [load("hart_order.json")])

    def boom():
        raise so.ShippedOrdersError("`ezship categories` failed (exit 1)")

    monkeypatch.setattr(eb, "load_live_categories", boom)
    assert h.main() == 1 and h.calls == []


def test_live_categories_parse_and_expired_cookie(monkeypatch):
    snapshot = (Path(eb.__file__).with_name("ezship_categories.json")).read_text()

    class Done:
        def __init__(self, code, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    cats = eb.load_live_categories(lambda cmd, **kw: Done(0, snapshot))
    assert cats == eb.load_categories()
    with pytest.raises(eb.EzshipSessionExpired):
        eb.load_live_categories(lambda cmd, **kw: Done(3, err="Session expired"))
    with pytest.raises(so.ShippedOrdersError, match="no category list"):
        eb.load_live_categories(lambda cmd, **kw: Done(0, "{}"))


@pytest.mark.parametrize("code, state", [
    (None, "unconfirmed exit=None"),          # timeout
    (-1, "failed exit=-1"),                   # killed by a signal: not "never launched"
])
def test_timeout_and_signal_keep_the_marker(monkeypatch, tmp_path, code, state):
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")],
                codes=[code])
    assert h.main() in (1, 4)
    assert json.loads(h.inflight.read_text())[HART_TRACKING]["state"] == state
    assert h.pings[-1][0] is False


def test_launch_failure_clears_the_marker_and_exits_1(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")])
    monkeypatch.setattr(eb, "run_submit_command",
                        lambda fields: (eb.LAUNCH_FAILED, "no npx"))
    assert h.main() == 1
    assert json.loads(h.inflight.read_text()) == {}


def test_nonzero_exit_that_wrote_the_ledger_is_unconfirmed(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")])

    def run(fields):
        h.ledger.write_text(json.dumps({fields["tracking"]: {}}))
        return 1, "connection dropped"

    monkeypatch.setattr(eb, "run_submit_command", run)
    assert h.main() == 1
    assert "unconfirmed" in json.loads(h.inflight.read_text())[HART_TRACKING]["state"]


def test_summary_line_prints_when_the_run_fails(monkeypatch, tmp_path, capsys):
    # The wrapper raises the hold alert from this line, whatever the exit code.
    h = Harness(monkeypatch, tmp_path, [with_item(load("hart_order.json"), "Logo T-shirt")],
                codes=[1])
    monkeypatch.setattr(eb, "attention_items", lambda report: [("k", "held order")])
    assert h.main() == 4
    assert "ezship-bound: submitted=0 attention=1 new_attention=1" in capsys.readouterr().out


def test_first_failure_stops_the_batch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(eb, "run_submit_command", lambda f: (calls.append(f), (1, "no"))[1])
    monkeypatch.setattr(eb, "ping_submit_heartbeat", lambda *a: None)
    monkeypatch.setattr(ss, "notify", lambda m: None)
    rows = [{"status": eb.READY, "merchant": "M", "order_number": str(i),
             "proposal": {"tracking": f"T{i}", "category": "Socks", "category_id": "875",
                          "declared_value": "$2.00"}} for i in range(2)]
    args = type("A", (), {"ledger": tmp_path / "ledger.json"})()
    inflight_path = tmp_path / "inflight.json"
    code = eb._submit_ready({"orders": rows}, args, "", {}, inflight_path, {"submitted": 0})
    assert code == 4 and [c["tracking"] for c in calls] == ["T0"]
    assert set(json.loads(inflight_path.read_text())) == {"T0"}   # T1 untouched


def test_unexpected_crash_pings_failure(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, [load("hart_order.json")])

    def boom(*a, **k):
        raise KeyError("surprise")

    monkeypatch.setattr(eb, "build_report", boom)
    assert h.main() == 1 and h.pings[-1][0] is False


# ─── Category matching ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, expected", [
    ("Hart Audio XLR Cable 10ft", ("1013", "Cables")),
    ("Wool Socks - Black / M", ("875", "Socks")),
    ("Batman T-shirt", ("869", "Tops & Tee")),
    ("Baseball Cap - Navy", ("1086", "Hat / Cap")),
    ("USB-C Charger Cable", ("1019", "Charger Cable")),   # longest name wins
    ("Bath Towel", ("832", "Bath Towel")),
    ("Leather Wallet", ("1077", "Wallet / Purse")),        # part of a compound name
    ("Board Game: Catan", ("891", "Board Games")),
    ("Plush Pikachu", ("1094", "soft toys")),              # alias
    ("Canvas Tote Bag", ("1075", "Handbag / Totebag")),    # alias
    ("Batman #1 comic", ("1063", "Books")),
    ("Exercise Book A5", ("1066", "Excercise Book")),      # alias for EZShip's spelling
    ("Wireless Mouse", ("1178", "Mouse")),                 # alias; bare "mouse" is held
])
def test_full_list_matches(name, expected):
    assert eb.pick_category([name])[:2] == expected


@pytest.mark.parametrize("name", [
    "Funko Pop Spider-Man",          # nothing in the list
    "Lord of the Rings Box Set",     # "ring" and "box" are too generic
    "Light-up Figure",
    "Hand Soap",                     # two subcategories share the name
    # Comic titles with a category word (review of BUI-1425).
    "The Umbrella Academy #1", "Mickey Mouse #1", "Witchblade #1 Top Cow",
    "Black Mirror", "Spider-Man Uniform", "Mouse Guard Vol. 1",
    # Comic-shop merchandise carrying a Books keyword.
    "Batman Statue", "Funko Pop Batman Figure", "Comic Art Print", "Batman Mug",
    "Comic Book Storage Bin", "Batman Poster",
])
def test_doubtful_items_get_no_category(name):
    assert eb.pick_category([name])[0] is None


def test_compound_part_does_not_steal_a_whole_name():
    # "Adapters & Cables" (1198) must not also claim "cable" from Cables (1013).
    assert eb.pick_category(["Coaxial cable"])[:2] == ("1013", "Cables")


def test_every_alias_id_is_in_the_saved_list():
    cats = eb.load_categories()
    for alias, cid in eb.CATEGORY_ALIASES.items():
        assert cid in cats, alias


def test_minimum_above_the_order_total_is_held():
    cats = eb.load_categories()
    # Electronic organ has a $20.00 minimum; the order cost $15.00.
    order = {"items": [{"name": "Electronic organ", "qty": 1}], "total_cents": 1500,
             "merchant": "M", "order_number": "1"}
    status, fields, why = eb.propose(order, {"tracking_number": "T", "carrier": "USPS"}, cats)
    assert status == eb.NO_CATEGORY and fields is None and "above the order total" in why
