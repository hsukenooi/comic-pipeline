"""Tests for slab_alerts.py (BUI-1419): pricing, gates, seen-set, Telegram,
and the scan wiring with every network edge faked."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import ebay_fetch
import slab_alerts as sa

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
CFG = {"margin": 0.10, "tax_rate": 0.0, "default_shipping": 15.0, "max_books": 0,
       "max_item_fetches": 40}
BOOK = {"comic_id": 7, "title": "Amazing Spider-Man", "issue": "23", "year": 1965,
        "min_grade": 6.0, "ceiling": 1500.0}


def summary(item_id="111111111111", title="Amazing Spider-Man #23 CGC 6.5 1965",
            price="900.00", options=("FIXED_PRICE",), ship="10.00", end=None):
    raw = {"itemId": f"v1|{item_id}|0", "title": title, "buyingOptions": list(options),
           "price": {"value": price, "currency": "USD"},
           "seller": {"username": "s1"}}
    if ship is not None:
        raw["shippingOptions"] = [{"shippingCost": {"value": ship, "currency": "USD"}}]
    if end:
        raw["itemEndDate"] = end
        raw["currentBidPrice"] = {"value": price, "currency": "USD"}
    return ebay_fetch.parse_item_summary(raw)


def full_item(returns=True, pct="99.9", score=900):
    return {"seller": {"feedbackPercentage": pct, "feedbackScore": score},
            "returnTerms": {"returnsAccepted": returns},
            "localizedAspects": [{"name": "Certification Number", "value": "123"}]}


def fmv(median=1200.0, conf="MEDIUM", flag=None, max_bid=1100):
    return {"median": median, "confidence": conf, "flag_reason": flag, "max_bid": max_bid}


ITEM_INPUT = {"item_id": "111111111111", "title": "Amazing Spider-Man", "issue": "23",
              "year": 1965, "grade": 6.5, "certifier": "cgc", "label": "universal"}


def error_row():
    """fmv_runner._graded_error_row with the BUI-536 fetch-err message."""
    return {"input": ITEM_INPUT, "fmv": None, "comp_count_total": 0, "queries_used": [],
            "db_row": None, "comic_id": None, "fmv_id": None, "source": "error",
            "breaker_tripped": False,
            "error": "fetch-err: all tiers failed; fmv DB row left untouched (BUI-536)"}


def advisory_row():
    """The graded BUI-663 advisory row (fmv_runner ~3180): the error row,
    re-sourced, with a priced band whose max_bid is withheld."""
    band = dict(fmv(conf="MEDIUM", max_bid=None), fmv_low=1000.0, fmv_high=1400.0,
                ledger_advisory=True, live_slab_count=0, ledger_slab_count=4)
    return {**error_row(), "fmv": band, "source": "ledger-advisory",
            "ledger_comic_id": 7, "ledger_rows": 4,
            "error": "fetch-err: all tiers failed; fmv DB row left untouched (BUI-536). "
                     "ADVISORY band priced from the SLAB comps ledger instead "
                     "(BUI-663/930): NOT a live price, NOT persisted, and it carries "
                     "NO max_bid."}


# --- parse + pricing -------------------------------------------------------


def test_summary_parse_tolerates_missing_fields():
    s = ebay_fetch.parse_item_summary({"itemId": "v1|123456|0"})
    assert s["shipping"] is None and s["best_offer"] is False
    s = summary(options=("FIXED_PRICE", "BEST_OFFER"), ship=None)
    assert s["best_offer"] and s["shipping"] is None
    bad = {"shippingOptions": [{"shippingCost": {"value": "x"}}, "junk",
                               {"shippingCost": {"value": "5", "currency": "GBP"}}]}
    assert ebay_fetch.summary_shipping_usd(bad) is None


def test_calculated_shipping_is_not_free():
    calc = {"shippingCostType": "CALCULATED",
            "shippingCost": {"value": "0.00", "currency": "USD"}}
    assert ebay_fetch.summary_shipping_usd({"shippingOptions": [calc]}) is None
    assert ebay_fetch.summary_shipping_usd({"shippingOptions": [
        calc, {"shippingCostType": "FIXED"},
        {"shippingCostType": "FIXED", "shippingCost": {"value": "12.50", "currency": "USD"}},
    ]}) == 12.5
    # a calculated-only copy falls back to the default shipping estimate
    raw = {"itemId": "v1|123456|0", "price": {"value": "100", "currency": "USD"},
           "shippingOptions": [calc]}
    s = ebay_fetch.parse_item_summary(raw)
    assert s["shipping"] is None
    assert sa.expected_price({"price": 100.0, "shipping": s["shipping"],
                              "listing_type": "BIN"}, CFG) == (115.0, 15.0)


def test_expected_price_bin_best_offer_tax_and_default_shipping():
    cfg = dict(CFG, tax_rate=0.10)
    assert sa.expected_price({"price": 100.0, "shipping": 10.0, "listing_type": "BIN"},
                             cfg) == (121.0, 10.0)
    assert sa.expected_price({"price": 100.0, "shipping": None, "best_offer": True,
                              "listing_type": "BIN"}, cfg) == (114.4, 15.0)
    # an auction's best_offer never scales its current bid
    assert sa.expected_price({"price": 100.0, "shipping": 0.0, "best_offer": True,
                              "listing_type": "Auction"}, CFG)[0] == 100.0
    assert sa.item_cap(121.0, 10.0, cfg) == 100.0


@pytest.mark.parametrize("row,ok", [
    (fmv(), True), (fmv(conf="MEDIUM-HIGH"), True), (fmv(conf="high"), True),
    (fmv(conf="MEDIUM-LOW"), False), (fmv(conf="LOW"), False), (fmv(conf=None), False),
    (fmv(flag="too_wide"), False), (fmv(median=None), False), (fmv(median=0), False),
    (fmv(max_bid=None), False), (dict(fmv(), ledger_advisory=True), False),
    (dict(fmv(max_bid=None), bin_listing=True), True),
    (dict(fmv(max_bid=None, conf="LOW"), bin_listing=True), False),
    (dict(fmv(max_bid=None), bin_listing=True, ledger_advisory=True), False),
    ({}, False),
])
def test_market_gate(row, ok):
    assert (sa.market_of(row)[0] is not None) is ok


def bin_row():
    """fmv_runner._graded_upsert_row for a BIN listing: `_apply_bin_rule` (R34)
    nulls max_bid and sets bin_listing; the band and confidence stand."""
    inp = dict(ITEM_INPUT, listing_type="BIN")
    band = dict(fmv(conf="MEDIUM", max_bid=None), fmv_low=1000.0, fmv_high=1400.0,
                graded=True, bin_listing=True)
    return {"input": inp, "fmv": band, "comp_count_total": 6, "queries_used": [],
            "db_row": {"id": 1}, "comic_id": 7, "fmv_id": 1, "source": "fresh",
            "breaker_tripped": False, "slab_comps": [], "comps_posted": 0}


def test_bin_row_is_priced_without_max_bid(monkeypatch):
    """BUI-1423: a real BIN row shape through run_fmv prices and alerts."""
    class P:
        returncode, stderr = 0, ""

    def run(cmd, **k):
        with open(cmd[cmd.index("--out") + 1], "w") as f:
            json.dump([bin_row()], f)
        return P()
    monkeypatch.setattr(sa.subprocess, "run", run)
    fmvs = sa.run_fmv([{"item_id": "111111111111"}], probe=False)
    assert sa.market_of(fmvs["111111111111"]) == (1200.0, "MEDIUM", None)
    c = {"item_id": "111111111111", "expected": 500.0, "ceiling": 1500.0}
    assert sa.gate([c], fmvs, CFG) == [c] and c["market"] == 1200.0


@pytest.mark.parametrize("row", [advisory_row(), error_row(),
                                 dict(advisory_row(), fmv=dict(fmv(), ledger_advisory=False)),
                                 dict(error_row(), fmv=fmv())])
def test_degraded_fmv_rows_are_unpriced(row, monkeypatch, tmp_path):
    """Real comic-fmv row shapes through run_fmv: an advisory or error row
    exits 0 yet never prices, even when its band looks alertable."""
    class P:
        returncode, stderr = 0, ""

    def run(cmd, **k):
        out = cmd[cmd.index("--out") + 1]
        with open(out, "w") as f:
            json.dump([row], f)
        return P()
    monkeypatch.setattr(sa.subprocess, "run", run)
    fmvs = sa.run_fmv([{"item_id": "111111111111"}], probe=False)
    market, _conf, skip = sa.market_of(fmvs["111111111111"])
    assert market is None and skip.startswith("comic-fmv ")
    c = {"item_id": "111111111111", "expected": 10.0, "ceiling": 1500.0}
    assert sa.gate([c], fmvs, CFG) == [] and c["unpriced"] and c["fmv_failed"]


def test_good_fmv_row_prices(monkeypatch):
    good = {"input": ITEM_INPUT, "fmv": fmv(), "source": "fresh", "error": None}
    assert sa.market_of(sa.fmv_of_row(good))[0] == 1200.0


def test_failed_fmv_never_reads_cheap():
    c = {"item_id": "1", "expected": 10.0, "ceiling": 1500.0}
    assert sa.gate([c], {}, CFG) == []


def test_margin_gate_boundary():
    mk = lambda e: {"item_id": "1", "expected": e, "ceiling": 1500.0}  # noqa: E731
    assert sa.gate([mk(1080.0)], {"1": fmv()}, CFG)[0]["discount"] == 0.1
    assert sa.gate([mk(1080.01)], {"1": fmv()}, CFG) == []
    assert sa.gate([mk(900.0)], {"1": fmv()}, dict(CFG, margin=0.3)) == []


# --- seen-set --------------------------------------------------------------


def test_needs_alert_rules():
    c = {"expected": 1000.0, "best_offer": False, "listing_type": "BIN"}
    assert sa.needs_alert(c, None, NOW)
    assert sa.needs_alert(c, {"last_alerted_at": None}, NOW)
    prev = {"last_alerted_at": "x", "alerted_expected": 1000.0,
            "alerted_best_offer": 0, "alerted_last_day": 0}
    assert not sa.needs_alert(c, prev, NOW)
    assert sa.needs_alert(dict(c, expected=990.0), prev, NOW)
    assert not sa.needs_alert(dict(c, expected=1010.0), prev, NOW)
    assert sa.needs_alert(dict(c, best_offer=True), prev, NOW)
    auction = dict(c, listing_type="Auction",
                   end_date=(NOW + timedelta(hours=5)).isoformat())
    assert sa.needs_alert(auction, prev, NOW)
    assert not sa.needs_alert(auction, dict(prev, alerted_last_day=1), NOW)
    far = dict(auction, end_date=(NOW + timedelta(days=3)).isoformat())
    assert not sa.needs_alert(far, prev, NOW)


def test_scan_book_ended_and_reseen():
    prev = {"222222222222": {"comic_id": 7, "ended_at": None},
            "111111111111": {"comic_id": 7, "ended_at": "2026-11-10T00:00:00Z"},
            "333333333333": {"comic_id": 8, "ended_at": None}}
    obs, elig, ended = sa.scan_book(BOOK, CFG, prev, NOW, search=lambda *a, **k: [summary()])
    assert [o["item_id"] for o in obs] == ["111111111111"]
    assert "ended_at" in obs[0] and obs[0]["ended_at"] is None
    assert ended == [{"item_id": "222222222222", "ended_at": NOW.isoformat()}]
    assert [e["item_id"] for e in elig] == ["111111111111"]


def test_scan_book_capped_search_ends_nothing(monkeypatch):
    monkeypatch.setattr(sa.slab_deals, "DEFAULT_MAX_RESULTS", 1)
    prev = {"222222222222": {"comic_id": 7, "ended_at": None}}
    _, _, ended = sa.scan_book(BOOK, CFG, prev, NOW, search=lambda *a, **k: [summary()])
    assert ended == []


def test_scan_book_grade_and_ceiling_filters():
    low = summary(title="Amazing Spider-Man #23 CGC 5.5 1965")
    pricey = summary("111111111112", price="1600.00")
    _, elig, _ = sa.scan_book(BOOK, CFG, {}, NOW, search=lambda *a, **k: [low, pricey])
    assert elig == []


# --- checks + render -------------------------------------------------------


def _passed(**kw):
    c = {"item_id": "111111111111", "book": "Amazing Spider-Man #23", "grade": 6.5,
         "listing_type": "BIN", "best_offer": False, "price": 900.0, "shipping": 10.0,
         "ship_used": 10.0, "expected": 910.0, "market": 1200.0, "threshold": 1080.0,
         "ceiling": 1500.0, "confidence": "MEDIUM", "discount": 0.2417,
         "url": "https://www.ebay.com/itm/111111111111"}
    c.update(kw)
    return c


def test_checks_flag_no_returns_and_drop_weak_seller():
    a = _passed()
    b = _passed(item_id="111111111112", discount=0.5)
    fetched = {"111111111111": full_item(returns=False), "111111111112": full_item(pct="90")}
    out = sa.check_copies([a, b], CFG, fetched.get)
    assert [c["item_id"] for c in out] == ["111111111111"]
    text, markup, shown = sa.render(out, "This month's spend: $0 (no cap)", CFG, NOW)
    assert "no returns" in text and "Walk away above $1,070" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "sa:act:111111111111"
    assert shown == out


def test_checks_failed_fetch_never_alerts():
    stats = {}
    assert sa.check_copies([_passed()], CFG, lambda _id: None, stats=stats) == []
    assert stats["fetch_failed"] == 1

    def boom(_id):
        raise ConnectionError("eBay down")
    assert sa.check_copies([_passed()], CFG, boom, stats=stats) == []
    assert stats["fetch_failed"] == 2


@pytest.mark.parametrize("aspect,kept", [
    ("6.5", True), ("FN+ 6.5", True), ("6.5 FN+", True), (None, True), ("Ungraded", True),
    ("7.0", False), ("VF 8.0", False),
])
def test_checks_grade_aspect_must_match_title_grade(aspect, kept):
    item = full_item()
    if aspect is not None:
        item["localizedAspects"].append({"name": "Grade", "value": aspect})
    stats = {}
    out = sa.check_copies([_passed()], CFG, lambda _id: item, stats=stats)
    assert bool(out) is kept and stats["grade_mismatch"] == (0 if kept else 1)


def test_aspect_grade_parse():
    assert sa.aspect_grade("NM/M 9.8") == 9.8 and sa.aspect_grade("10") == 10.0
    assert sa.aspect_grade("9.8 / 9.6") is None and sa.aspect_grade("") is None


def test_checks_reprice_from_item_shipping_regates_seen_set():
    c = _passed(shipping=None, ship_used=15.0, expected=915.0)
    item = dict(full_item(), shippingOptions=[{"shippingCost": {"value": "25.00"}}])
    prev = {"111111111111": {"last_alerted_at": "x", "alerted_expected": 925.0,
                             "alerted_best_offer": 0, "alerted_last_day": 0}}
    assert sa.check_copies([dict(c)], CFG, lambda _id: item, prev, NOW) == []
    out = sa.check_copies([dict(c)], CFG, lambda _id: item, {}, NOW)
    assert out[0]["expected"] == 925.0


def test_render_budget_leaves_rest_unshown(monkeypatch):
    monkeypatch.setattr(sa, "MESSAGE_BUDGET", 500)
    alerts = [dict(_passed(item_id=str(111111111110 + i)),
                   checks={"verdict": "pass", "flags": []}) for i in range(5)]
    text, markup, shown = sa.render(alerts, "spend", CFG, NOW)
    assert 0 < len(shown) < 5 and len(text) <= 600 and "more next scan" in text
    assert len(markup["inline_keyboard"]) == len(shown)


# --- Telegram ---------------------------------------------------------------


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def test_telegram_429_retry_then_ok(monkeypatch):
    calls = iter([Resp(429, {"ok": False, "parameters": {"retry_after": 2}}),
                  Resp(200, {"ok": True, "result": 5})])
    monkeypatch.setattr(sa.requests, "post", lambda *a, **k: next(calls))
    slept = []
    assert sa.telegram_call("T", "sendMessage", {}, sleep=slept.append) == 5
    assert slept == [2]


def test_telegram_long_429_and_errors_hide_token(monkeypatch):
    monkeypatch.setattr(sa.requests, "post",
                        lambda *a, **k: Resp(429, {"parameters": {"retry_after": 600}}))
    with pytest.raises(sa.SlabAlertError):
        sa.telegram_call("T", "sendMessage", {}, sleep=lambda s: None)

    def boom(url, **k):
        raise sa.requests.exceptions.ConnectionError(f"failed {url}")
    monkeypatch.setattr(sa.requests, "post", boom)
    with pytest.raises(sa.SlabAlertError) as e:
        sa.telegram_call("SECRET123", "getUpdates", {})
    assert "SECRET123" not in str(e.value)
    monkeypatch.setattr(sa.requests, "post", lambda *a, **k: Resp(502, ValueError()))
    with pytest.raises(sa.SlabAlertError):
        sa.telegram_call("T", "getUpdates", {})


def test_parse_ratings_filters_chat_and_format():
    ups = [
        {"update_id": 10, "callback_query": {"id": "a", "data": "sa:act:111111111111",
                                             "message": {"chat": {"id": 42}}}},
        {"update_id": 11, "callback_query": {"id": "b", "data": "sa:pass:111111111111",
                                             "message": {"chat": {"id": 99}}}},
        {"update_id": 12, "message": {"text": "hi"}},
        {"update_id": 13, "callback_query": {"id": "c", "data": "other"}},
        "junk",
    ]
    ratings, answers, offset = sa.parse_ratings(ups, "42")
    assert ratings == [{"item_id": "111111111111", "rating": "act"}]
    assert answers == ["a", "b", "c"] and offset == 14
    assert sa.parse_ratings(None, "42") == ([], [], None)


def test_process_ratings_offset_only_after_store(monkeypatch, tmp_path):
    state = tmp_path / "s.json"
    ups = [{"update_id": 5, "callback_query": {"id": "a", "data": "sa:act:111111111111",
                                               "message": {"chat": {"id": 42}}}}]
    calls = []
    monkeypatch.setattr(sa, "telegram_call",
                        lambda t, m, p, **k: calls.append((m, p)) or (ups if m == "getUpdates" else True))

    def fail(*a):
        raise sa.SlabAlertError("server down")
    monkeypatch.setattr(sa, "_post", fail)
    warnings = []
    # a failed store warns, keeps the offset (the presses replay), answers nothing
    assert sa.process_ratings("http://x", "T", "42", state, warnings) == 0
    assert not state.exists() and warnings == ["1 ratings not stored: server down"]
    assert [m for m, _ in calls] == ["getUpdates"]
    monkeypatch.setattr(sa, "_post", lambda *a: {"recorded": 1})
    assert sa.process_ratings("http://x", "T", "42", state) == 1
    assert json.loads(state.read_text()) == {"telegram_offset": 6}
    sa.process_ratings("http://x", "T", "42", state)
    assert calls[-2] == ("getUpdates", {"timeout": 0, "offset": 6})


def test_telegram_creds_reads_only_two_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    f = tmp_path / "env"
    f.write_text('LINEAR_KEY=x\nTELEGRAM_BOT_TOKEN="abc"\nexport TELEGRAM_CHAT_ID=42\n')
    assert sa.telegram_creds(f) == ("abc", "42")
    f.write_text("LINEAR_KEY=x\n")
    with pytest.raises(sa.SlabAlertError):
        sa.telegram_creds(f)


def test_bad_env_is_error(monkeypatch):
    monkeypatch.setenv("SLAB_ALERT_MARGIN", "1.5")
    with pytest.raises(sa.SlabAlertError):
        sa.config()


# --- scan wiring --------------------------------------------------------------


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("COMICS_SERVER_URL", "http://srv")
    monkeypatch.setenv("SLAB_ALERT_TAX_RATE", "0")
    monkeypatch.setenv("SLAB_ALERT_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(sa.slab_deals, "_health_gate", lambda base: None)
    rec = {"posts": [], "tg": [], "prev": []}

    def get(base, path, params=None):
        if path == "/api/comics/slab-watch":
            return {"items": [BOOK, dict(BOOK, comic_id=8, min_grade=None)]}
        if path == "/api/comics/slab-alerts":
            return {"copies": rec["prev"]}
        if path == "/api/comics/purchases/spend":
            return {"months": [{"month": "2026-11", "currency": "USD", "total": 450.0}]}
        raise AssertionError(path)
    monkeypatch.setattr(sa, "_get", get)
    monkeypatch.setattr(sa, "_post", lambda base, path, body: rec["posts"].append((path, body)) or {})

    def tg(token, method, payload, **k):
        rec["tg"].append((method, payload))
        return [] if method == "getUpdates" else {"message_id": 1}
    monkeypatch.setattr(sa, "telegram_call", tg)
    return rec


def _scan(rec, *, dry_run=False, fmv_fn=None, **kw):
    out = []
    summary_ = sa.scan(dry_run=dry_run, now=NOW, search=lambda *a, **k: [summary()],
                       fetch=lambda _id: full_item(returns=False),
                       fmv=fmv_fn or (lambda rows, probe: {r["item_id"]: fmv() for r in rows}),
                       out=out.append, **kw)
    return summary_, out


def test_scan_sends_one_message_then_marks_alerted(server):
    s, _ = _scan(server)
    assert s["books"] == 1 and s["alerts"] == 1
    methods = [m for m, _ in server["tg"]]
    assert methods == ["getUpdates", "sendMessage"]
    text = server["tg"][1][1]["text"]
    assert "$450" in text and "no returns" in text
    paths = [p for p, _ in server["posts"]]
    assert paths == ["/api/comics/slab-alerts", "/api/comics/slab-alerts"]
    mark = server["posts"][1][1]["copies"][0]
    assert mark["alerted_expected"] == 910.0 and mark["last_alerted_at"]


def test_send_only_mode_skips_getupdates_and_buttons(server, monkeypatch):
    monkeypatch.setenv("SLAB_ALERT_READ_BUTTONS", "0")
    s, _ = _scan(server)
    assert s["alerts"] == 1 and s["ratings"] == 0
    assert [m for m, _ in server["tg"]] == ["sendMessage"]
    assert "reply_markup" not in server["tg"][0][1]


def test_read_buttons_rejects_garbage(monkeypatch):
    monkeypatch.setenv("SLAB_ALERT_READ_BUTTONS", "maybe")
    with pytest.raises(sa.SlabAlertError):
        sa.read_buttons()


def test_scan_does_not_realert_unchanged_copy(server):
    server["prev"] = [{"item_id": "111111111111", "comic_id": 7, "ended_at": None,
                       "last_alerted_at": "x", "alerted_expected": 910.0,
                       "alerted_best_offer": 0, "alerted_last_day": 0}]
    priced = []
    s, _ = _scan(server, fmv_fn=lambda rows, probe: priced.extend(rows) or {})
    assert s["alerts"] == 0 and priced == []
    assert [m for m, _ in server["tg"]] == ["getUpdates"]


def test_send_failure_leaves_copy_unalerted(server, monkeypatch):
    def tg(token, method, payload, **k):
        if method == "sendMessage":
            raise sa.SlabAlertError("telegram sendMessage rate-limited")
        return []
    monkeypatch.setattr(sa, "telegram_call", tg)
    with pytest.raises(sa.SlabAlertError):
        _scan(server)
    assert all("last_alerted_at" not in c for _, b in server["posts"] for c in b["copies"])


def test_fmv_failure_is_hard(server):
    def bad(rows, probe):
        raise sa.SlabAlertError("comic-fmv exited 1")
    with pytest.raises(sa.SlabAlertError):
        _scan(server, fmv_fn=bad)
    assert not any(m == "sendMessage" for m, _ in server["tg"])


def test_dry_run_writes_nothing(server):
    probes = []
    s, out = _scan(server, dry_run=True,
                   fmv_fn=lambda rows, probe: probes.append(probe) or
                   {r["item_id"]: fmv() for r in rows})
    assert server["posts"] == [] and server["tg"] == [] and probes == [True]
    assert "Slab alerts: 1" in out[0] and s["alerts"] == 1


def test_low_confidence_sends_nothing(server):
    s, _ = _scan(server, fmv_fn=lambda rows, probe: {r["item_id"]: fmv(conf="LOW")
                                                     for r in rows})
    assert s["alerts"] == 0 and [m for m, _ in server["tg"]] == ["getUpdates"]
    assert len(server["posts"]) == 1  # observations only


def test_unreachable_server_hard_fails(monkeypatch):
    monkeypatch.setenv("COMICS_SERVER_URL", "http://srv")

    def down(base):
        raise sa.slab_deals.SlabDealsError("not responding")
    monkeypatch.setattr(sa.slab_deals, "_health_gate", down)
    with pytest.raises(sa.SlabAlertError):
        sa.scan(dry_run=True)


def test_run_fmv_nonzero_exit_is_hard(monkeypatch):
    class P:
        returncode, stderr = 2, "boom"
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: P())
    with pytest.raises(sa.SlabAlertError):
        sa.run_fmv([{"item_id": "1"}], probe=True)
    assert sa.run_fmv([], probe=False) == {}


# --- failure notice -----------------------------------------------------------


def _main(monkeypatch, scan_fn, argv=()):
    monkeypatch.setattr(sa, "scan", scan_fn)
    with pytest.raises(SystemExit) as e:
        sa.main(list(argv))
    return e.value.code


def _summary(**kw):
    s = {"books": 3, "copies": 4, "eligible": 2, "unpriced": 0, "fmv_failed": 0,
         "priced_pass": 1, "fetch_failed": 0, "grade_mismatch": 0, "alerts": 1,
         "ratings": 0, "errors": [], "warnings": []}
    s.update(kw)
    return s


@pytest.mark.parametrize("exc", [sa.SlabAlertError("GET /api/comics/slab-watch HTTP 500"),
                                 SystemExit(1), KeyError("copies")])
def test_failed_scan_sends_notice(server, monkeypatch, exc):
    def bad(**k):
        raise exc
    assert _main(monkeypatch, bad) == 1
    (method, payload), = server["tg"]
    assert method == "sendMessage" and payload["text"].startswith("Slab alerts scan failed.")
    assert "reply_markup" not in payload


def test_partial_scan_notice_has_counts(server, monkeypatch):
    s = _summary(errors=["X #1: browse 503"], fetch_failed=2, unpriced=3, fmv_failed=1,
                 warnings=["1 ratings not stored: down"])
    assert _main(monkeypatch, lambda **k: s) == 2
    text = server["tg"][0][1]["text"]
    for part in ("ran partial", "1 of 3 searches failed", "browse 503", "2 getItem failed",
                 "1 comic-fmv failed", "3 unpriced", "ratings not stored", "1 alerts sent"):
        assert part in text


def test_clean_scan_sends_no_notice(server, monkeypatch):
    monkeypatch.setattr(sa, "scan", lambda **k: _summary(unpriced=2, grade_mismatch=1))
    sa.main([])
    assert server["tg"] == []


def test_notice_send_failure_goes_to_stderr(server, monkeypatch, capsys):
    def tg(*a, **k):
        raise sa.SlabAlertError("telegram sendMessage HTTP 401: Unauthorized")
    monkeypatch.setattr(sa, "telegram_call", tg)

    def bad(**k):
        raise sa.SlabAlertError("comic-fmv exited 1")
    assert _main(monkeypatch, bad) == 1
    err = capsys.readouterr().err
    assert "comic-fmv exited 1" in err and "failure notice not sent" in err


def test_dry_run_failure_sends_no_notice(server, monkeypatch):
    def bad(**k):
        raise sa.SlabAlertError("boom")
    assert _main(monkeypatch, bad, ["--dry-run"]) == 1
    assert server["tg"] == []


def test_scan_counts_unpriced_and_fetch_failed(server):
    s, _ = _scan(server, fmv_fn=lambda rows, probe: {
        r["item_id"]: sa.fmv_of_row(advisory_row()) for r in rows})
    assert s["unpriced"] == 1 and s["fmv_failed"] == 1 and s["alerts"] == 0
    assert [m for m, _ in server["tg"]] == ["getUpdates"]
    assert sa.partial_problems(s)
    # dry run: the first scan still holds the process lock
    s = sa.scan(dry_run=True, now=NOW, search=lambda *a, **k: [summary("111111111119")],
                fetch=lambda _id: None, out=lambda *_: None,
                fmv=lambda rows, probe: {r["item_id"]: fmv() for r in rows})
    assert s["fetch_failed"] == 1 and s["alerts"] == 0
