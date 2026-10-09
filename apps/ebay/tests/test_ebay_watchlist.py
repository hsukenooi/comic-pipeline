"""Tests for ebay-watchlist (BUI-1210). Token and HTTP are mocked; XML is hand-written."""

import json

import pytest
import requests

import ebay_user_token as eut
import ebay_watchlist as wl

NSDECL = 'xmlns="urn:ebay:apis:eBLBaseComponents"'

# (item_id, title, listing_type, end_time, price, bids, seller)
AUCTION_LIVE = ("1001", "Test Comic A #1", "Chinese", "2099-01-01T12:00:00.000Z", "12.50", 3, "seller_a")
AUCTION_ENDED = ("1002", "Test Comic B #2", "Chinese", "2020-01-01T12:00:00.000Z", "8.00", 1, "seller_b")
BIN_LIVE = ("1003", "Test Comic C #3", "FixedPriceItem", "2099-02-01T12:00:00.000Z", "40.00", 0, "seller_c")
STORE_BIN_LIVE = ("1004", "Test Novel D", "StoresFixedPrice", "2099-03-01T12:00:00.000Z", "9.99", 0, "seller_d")
BIN_ENDED = ("1005", "Test Comic E #5", "FixedPriceItem", "2020-02-01T12:00:00.000Z", "5.00", 0, "seller_e")


def _item(t):
    iid, title, ltype, end, price, bids, seller = t
    return (
        f"<Item><ItemID>{iid}</ItemID><Title>{title}</Title><ListingType>{ltype}</ListingType>"
        f"<ListingDetails><EndTime>{end}</EndTime>"
        f"<ViewItemURL>https://www.ebay.com/itm/{iid}</ViewItemURL></ListingDetails>"
        f'<SellingStatus><CurrentPrice currencyID="USD">{price}</CurrentPrice>'
        f"<BidCount>{bids}</BidCount></SellingStatus>"
        f"<Seller><UserID>{seller}</UserID></Seller></Item>"
    )


def page_xml(items, page=1, total_pages=1, ack="Success", errors=(), omit_watchlist=False):
    errs = "".join(
        f"<Errors><ErrorCode>{c}</ErrorCode><LongMessage>{m}</LongMessage></Errors>"
        for c, m in errors
    )
    wl_xml = (
        f"<WatchList><PaginationResult><TotalNumberOfPages>{total_pages}</TotalNumberOfPages>"
        f"</PaginationResult><ItemArray>{''.join(_item(i) for i in items)}</ItemArray></WatchList>"
    )
    body = wl_xml if ack != "Failure" and not omit_watchlist else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><GetMyeBayBuyingResponse {NSDECL}>'
        f"<Ack>{ack}</Ack>{errs}{body}</GetMyeBayBuyingResponse>"
    ).encode()


class FakeResp:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status


class Calls(list):
    queue = None


@pytest.fixture
def http(monkeypatch):
    """Install a token mock and a queue of responses; returns the call log."""
    calls = Calls()
    queue = []

    monkeypatch.setattr(eut, "get_user_access_token", lambda: "fake-token")

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data.decode(), "headers": headers})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(wl.requests, "post", fake_post)
    calls.queue = queue
    return calls


def run(capsys, *argv):
    code = wl.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


ALL = [AUCTION_LIVE, AUCTION_ENDED, BIN_LIVE, STORE_BIN_LIVE, BIN_ENDED]


def test_default_is_live_only(http, capsys):
    http.queue.append(FakeResp(page_xml(ALL)))
    code, out, _ = run(capsys, "--json")
    assert code == 0
    assert [i["item_id"] for i in json.loads(out)] == ["1001", "1003", "1004"]


def test_include_ended(http, capsys):
    http.queue.append(FakeResp(page_xml(ALL)))
    code, out, _ = run(capsys, "--json", "--include-ended")
    assert [i["item_id"] for i in json.loads(out)] == ["1001", "1002", "1003", "1004", "1005"]


def test_type_auction(http, capsys):
    http.queue.append(FakeResp(page_xml(ALL)))
    _, out, _ = run(capsys, "--json", "--type", "auction", "--include-ended")
    assert [i["item_id"] for i in json.loads(out)] == ["1001", "1002"]


def test_type_bin_includes_store_fixed_price(http, capsys):
    http.queue.append(FakeResp(page_xml(ALL)))
    _, out, _ = run(capsys, "--json", "--type", "bin")
    assert [i["item_id"] for i in json.loads(out)] == ["1003", "1004"]


def test_type_all_explicit(http, capsys):
    http.queue.append(FakeResp(page_xml(ALL)))
    _, out, _ = run(capsys, "--json", "--type", "all")
    assert len(json.loads(out)) == 3


def test_pagination_reads_every_page(http, capsys):
    http.queue.append(FakeResp(page_xml([AUCTION_LIVE], page=1, total_pages=3)))
    http.queue.append(FakeResp(page_xml([BIN_LIVE], page=2, total_pages=3)))
    http.queue.append(FakeResp(page_xml([STORE_BIN_LIVE], page=3, total_pages=3)))
    code, out, _ = run(capsys, "--json")
    assert code == 0
    assert [i["item_id"] for i in json.loads(out)] == ["1001", "1003", "1004"]
    assert len(http) == 3
    for n, call in enumerate(http, start=1):
        assert f"<PageNumber>{n}</PageNumber>" in call["data"]
        assert "<EntriesPerPage>200</EntriesPerPage>" in call["data"]


def test_request_headers(http, capsys):
    http.queue.append(FakeResp(page_xml([AUCTION_LIVE])))
    run(capsys, "--json")
    h = http[0]["headers"]
    assert h["X-EBAY-API-IAF-TOKEN"] == "fake-token"
    assert h["X-EBAY-API-CALL-NAME"] == "GetMyeBayBuying"
    assert http[0]["url"] == "https://api.ebay.com/ws/api.dll"


def test_json_fields(http, capsys):
    http.queue.append(FakeResp(page_xml([AUCTION_LIVE])))
    _, out, _ = run(capsys, "--json")
    assert json.loads(out) == [
        {
            "item_id": "1001",
            "title": "Test Comic A #1",
            "listing_type": "Chinese",
            "end_time": "2099-01-01T12:00:00.000Z",
            "current_price": "12.50",
            "bid_count": 3,
            "seller": "seller_a",
            "url": "https://www.ebay.com/itm/1001",
        }
    ]


def test_urls(http, capsys):
    http.queue.append(FakeResp(page_xml(ALL)))
    _, out, _ = run(capsys, "--urls")
    assert out.splitlines() == [
        "https://www.ebay.com/itm/1001",
        "https://www.ebay.com/itm/1003",
        "https://www.ebay.com/itm/1004",
    ]


def test_table_default(http, capsys):
    http.queue.append(FakeResp(page_xml([AUCTION_LIVE])))
    _, out, _ = run(capsys)
    lines = out.splitlines()
    assert lines[0].startswith("item_id")
    assert "Test Comic A #1" in lines[1] and "seller_a" in lines[1]


def test_empty_watchlist_is_success(http, capsys):
    http.queue.append(FakeResp(page_xml([])))
    code, out, _ = run(capsys, "--json")
    assert code == 0
    assert json.loads(out) == []


def test_token_error_exits_3_no_stdout(monkeypatch, capsys):
    def boom():
        raise eut.UserTokenError("eBay user token not found; run `ebay-auth login`")

    monkeypatch.setattr(eut, "get_user_access_token", boom)
    code, out, err = run(capsys, "--json")
    assert code == 3
    assert out == ""
    assert "ebay-auth login" in err


def test_token_network_error_uses_its_own_exit_code(monkeypatch, capsys):
    def boom():
        raise eut.UserTokenError("network error", eut.EXIT_NETWORK)

    monkeypatch.setattr(eut, "get_user_access_token", boom)
    code, out, _ = run(capsys, "--json")
    assert code == 5
    assert out == ""


def test_ack_failure_exits_4_no_stdout(http, capsys):
    http.queue.append(
        FakeResp(page_xml([], ack="Failure", errors=[("10007", "Internal error.")]))
    )
    code, out, err = run(capsys, "--json")
    assert code == 4
    assert out == ""
    assert "10007" in err and "Internal error" in err


def test_ack_failure_on_later_page_prints_nothing(http, capsys):
    http.queue.append(FakeResp(page_xml([AUCTION_LIVE], total_pages=2)))
    http.queue.append(FakeResp(page_xml([], ack="Failure", errors=[("10007", "Internal error")])))
    code, out, _ = run(capsys, "--urls")
    assert code == 4
    assert out == ""


def test_http_error_exits_5_no_stdout(http, capsys):
    http.queue.append(FakeResp(b"oops", status=503))
    code, out, err = run(capsys, "--json")
    assert code == 5
    assert out == ""
    assert "503" in err


def test_connection_error_exits_5_no_stdout(http, capsys):
    http.queue.append(requests.ConnectionError("down"))
    code, out, err = run(capsys, "--urls")
    assert code == 5
    assert out == ""
    assert "ConnectionError" in err


def test_garbage_body_exits_5(http, capsys):
    http.queue.append(FakeResp(b"<html>not xml"))
    code, out, _ = run(capsys, "--json")
    assert code == 5
    assert out == ""


def test_ack_warning_with_items_is_success(http, capsys):
    http.queue.append(
        FakeResp(page_xml([AUCTION_LIVE], ack="Warning", errors=[("21917", "Deprecated field")]))
    )
    code, out, err = run(capsys, "--json")
    assert code == 0
    assert len(json.loads(out)) == 1
    assert "21917" in err


# BUI-1214: live eBay behavior found on the first deploy.


def test_type_auction_accepts_watchlist_auction_label(http, capsys):
    live = ("2001", "Watched Auction #1", "Auction", "2099-01-01T12:00:00.000Z", "1.81", 2, "s")
    http.queue.append(FakeResp(page_xml([live, BIN_LIVE])))
    _, out, _ = run(capsys, "--json", "--type", "auction")
    assert [i["item_id"] for i in json.loads(out)] == ["2001"]


def test_missing_watchlist_is_retried(http, capsys):
    http.queue.append(FakeResp(page_xml([], omit_watchlist=True)))
    http.queue.append(FakeResp(page_xml([], omit_watchlist=True)))
    http.queue.append(FakeResp(page_xml([AUCTION_LIVE])))
    code, out, _ = run(capsys, "--json")
    assert code == 0
    assert [i["item_id"] for i in json.loads(out)] == ["1001"]
    assert len(http) == 3


def test_persistently_missing_watchlist_exits_5_no_stdout(http, capsys):
    for _ in range(wl.MISSING_WATCHLIST_TRIES):
        http.queue.append(FakeResp(page_xml([], omit_watchlist=True)))
    code, out, err = run(capsys, "--json")
    assert code == 5
    assert out == ""
    assert "no WatchList element" in err
    assert len(http) == wl.MISSING_WATCHLIST_TRIES


def test_missing_watchlist_on_later_page_prints_nothing(http, capsys):
    http.queue.append(FakeResp(page_xml([AUCTION_LIVE], page=1, total_pages=2)))
    for _ in range(wl.MISSING_WATCHLIST_TRIES):
        http.queue.append(FakeResp(page_xml([], omit_watchlist=True)))
    code, out, _ = run(capsys, "--urls")
    assert code == 5
    assert out == ""


def test_page_cap_exceeded_fails_instead_of_truncating(http, capsys, monkeypatch):
    monkeypatch.setattr(wl, "MAX_PAGES", 2)
    http.queue.append(FakeResp(page_xml([AUCTION_LIVE], page=1, total_pages=3)))
    http.queue.append(FakeResp(page_xml([BIN_LIVE], page=2, total_pages=3)))
    code, out, err = run(capsys, "--json")
    assert code == 5
    assert out == ""
    assert "cap" in err


@pytest.mark.parametrize("code_", ["931", "932", "21916984", "21917053"])
def test_trading_auth_rejection_exits_3(http, capsys, code_):
    http.queue.append(FakeResp(page_xml([], ack="Failure", errors=[(code_, "token rejected")])))
    code, out, err = run(capsys, "--json")
    assert code == 3
    assert out == ""
    assert "ebay-auth login" in err
