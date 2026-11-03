"""Tests for ebay-bid-status (BUI-1346). Token and HTTP are mocked."""

import json

import pytest
import requests

import ebay_bid_status as bs
import ebay_user_token as eut

NSDECL = 'xmlns="urn:ebay:apis:eBLBaseComponents"'
TIE_ITEM = "188733995492"


def gql(status, value="25.0", currency="USD", converted=None):
    amount = {"original": {"value": value, "currency": currency}}
    if converted:
        amount["converted"] = {"value": converted, "currency": "USD"}
    return {"data": {"itemBiddingActivity": {"itemBiddingActivity": {
        "leadingBid": {"bidAmount": amount},
        "bidderAuctionDetails": {"userBiddingStatus": status},
        "item": {"itemId": TIE_ITEM},
    }}}}


FORBIDDEN = {"errors": [{"extensions": {"errorCode": "FORBIDDEN"},
                         "message": "Insufficient permissions to fulfill the request."}],
             "data": None}


def trading_xml(won=(), lost=(), ack="Success", pages=1):
    def items(ids):
        return "".join(
            f"<Item><ItemID>{i}</ItemID><SellingStatus>"
            f'<CurrentPrice currencyID="USD">25.00</CurrentPrice></SellingStatus></Item>'
            for i in ids
        )
    won_xml = (
        f"<WonList><PaginationResult><TotalNumberOfPages>{pages}</TotalNumberOfPages>"
        "</PaginationResult><OrderTransactionArray><OrderTransaction><Transaction>"
        f"{items(won)}</Transaction></OrderTransaction></OrderTransactionArray></WonList>"
    )
    lost_xml = f"<LostList><ItemArray>{items(lost)}</ItemArray></LostList>"
    body = won_xml + lost_xml if ack != "Failure" else "<Errors><ErrorCode>931</ErrorCode></Errors>"
    return (f'<?xml version="1.0"?><GetMyeBayBuyingResponse {NSDECL}><Ack>{ack}</Ack>'
            f"{body}</GetMyeBayBuyingResponse>").encode()


class FakeResp:
    def __init__(self, *, json_body=None, content=b"", status=200):
        self._json = json_body
        self.content = content
        self.status_code = status

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def http(monkeypatch):
    """Queue responses per endpoint; record calls."""
    queues = {bs.GRAPHQL_ENDPOINT: [], bs.TRADING_ENDPOINT: []}
    calls = []

    def post(url, **kw):
        calls.append(url)
        nxt = queues[url].pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(bs.requests, "post", post)
    monkeypatch.setattr(eut, "get_user_access_token", lambda: "tok")
    return queues, calls


def run(capsys, *ids):
    code = bs.main([*ids, "--json"])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out else None), out.err


def test_tie_reports_outbid_via_graphql(http, capsys):
    queues, calls = http
    queues[bs.GRAPHQL_ENDPOINT].append(FakeResp(json_body=gql("OUTBID")))
    code, res, _ = run(capsys, TIE_ITEM)
    assert code == 0
    assert res == [{"item_id": TIE_ITEM, "resolved": True, "status": "OUTBID",
                    "final_price": 25.0, "currency": "USD", "source": "itemBiddingActivity"}]
    assert calls == [bs.GRAPHQL_ENDPOINT]  # resolved: no Trading call


def test_null_status_is_resolved_none(http, capsys):
    http[0][bs.GRAPHQL_ENDPOINT].append(FakeResp(json_body=gql(None)))
    _, res, _ = run(capsys, TIE_ITEM)
    assert res[0]["resolved"] is True and res[0]["status"] is None


def test_converted_amount_preferred(http, capsys):
    http[0][bs.GRAPHQL_ENDPOINT].append(
        FakeResp(json_body=gql("WINNING", value="30", currency="GBP", converted="38.5")))
    _, res, _ = run(capsys, TIE_ITEM)
    assert res[0]["final_price"] == 38.5 and res[0]["currency"] == "USD"


def test_scope_error_falls_back_to_lostlist(http, capsys):
    """Base-scope token: GraphQL FORBIDDEN once, then Trading answers."""
    queues, calls = http
    queues[bs.GRAPHQL_ENDPOINT].append(FakeResp(json_body=FORBIDDEN))
    queues[bs.TRADING_ENDPOINT].append(FakeResp(content=trading_xml(won=["111"], lost=[TIE_ITEM])))
    code, res, err = run(capsys, TIE_ITEM, "111", "222")
    assert code == 0
    assert calls == [bs.GRAPHQL_ENDPOINT, bs.TRADING_ENDPOINT]  # GraphQL asked once only
    by = {r["item_id"]: r for r in res}
    assert by[TIE_ITEM]["status"] == "OUTBID" and by[TIE_ITEM]["source"] == "GetMyeBayBuying"
    assert by["111"]["status"] == "WINNING" and by["111"]["resolved"] is True
    # Absent from both lists is not evidence.
    assert by["222"]["resolved"] is False and by["222"]["status"] is None
    assert "unavailable" in err


def test_http_403_is_scope_failure(http, capsys):
    queues, _ = http
    queues[bs.GRAPHQL_ENDPOINT].append(FakeResp(json_body={}, status=403))
    queues[bs.TRADING_ENDPOINT].append(FakeResp(content=trading_xml()))
    code, res, _ = run(capsys, TIE_ITEM)
    assert code == 0 and res[0]["resolved"] is False


def test_every_source_failed_exits_5(http, capsys):
    queues, _ = http
    queues[bs.GRAPHQL_ENDPOINT].append(FakeResp(json_body=FORBIDDEN))
    queues[bs.TRADING_ENDPOINT].append(FakeResp(content=trading_xml(ack="Failure")))
    code, res, _ = run(capsys, TIE_ITEM)
    assert code == bs.EXIT_NETWORK and res is None


def test_network_error_exits_5(http, capsys):
    queues, _ = http
    queues[bs.GRAPHQL_ENDPOINT].append(requests.ConnectionError("down"))
    queues[bs.TRADING_ENDPOINT].append(requests.ConnectionError("down"))
    code, _, _ = run(capsys, TIE_ITEM)
    assert code == bs.EXIT_NETWORK


def test_per_item_graphql_error_is_unresolved_then_trading(http, capsys):
    queues, _ = http
    queues[bs.GRAPHQL_ENDPOINT].append(FakeResp(json_body={"errors": [{"message": "bad item"}], "data": None}))
    queues[bs.TRADING_ENDPOINT].append(FakeResp(content=trading_xml(lost=[TIE_ITEM])))
    _, res, _ = run(capsys, TIE_ITEM)
    assert res[0]["status"] == "OUTBID" and res[0]["source"] == "GetMyeBayBuying"


def test_unknown_status_is_unresolved(http, capsys):
    queues, _ = http
    queues[bs.GRAPHQL_ENDPOINT].append(FakeResp(json_body=gql("LEADING")))
    queues[bs.TRADING_ENDPOINT].append(FakeResp(content=trading_xml()))
    _, res, _ = run(capsys, TIE_ITEM)
    assert res[0]["resolved"] is False


def test_in_both_lists_is_unresolved():
    assert bs.trading_result("1", {"1": (1.0, "USD")}, {"1": (1.0, "USD")})["resolved"] is False


def test_trading_paginates(http, capsys):
    queues, calls = http
    queues[bs.GRAPHQL_ENDPOINT].append(FakeResp(json_body=FORBIDDEN))
    queues[bs.TRADING_ENDPOINT].append(FakeResp(content=trading_xml(pages=2)))
    queues[bs.TRADING_ENDPOINT].append(FakeResp(content=trading_xml(lost=[TIE_ITEM], pages=2)))
    _, res, _ = run(capsys, TIE_ITEM)
    assert calls.count(bs.TRADING_ENDPOINT) == 2 and res[0]["status"] == "OUTBID"


def test_token_error_exits_with_its_code(monkeypatch, capsys):
    def boom():
        raise eut.UserTokenError("expired; run `ebay-auth login`", eut.EXIT_RELOGIN)
    monkeypatch.setattr(eut, "get_user_access_token", boom)
    assert bs.main([TIE_ITEM, "--json"]) == eut.EXIT_RELOGIN
    assert capsys.readouterr().out == ""


def test_non_numeric_item_id_is_usage_error(capsys):
    assert bs.main(["abc", "--json"]) == bs.EXIT_USAGE


def test_never_calls_user_bidding_activity():
    assert "userBiddingActivity" not in bs.ITEM_QUERY
