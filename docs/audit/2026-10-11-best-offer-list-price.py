#!/usr/bin/env python3
"""BUI-1222: does sold-comps.com `soldPrice` hold the list price on best-offer sales?

DIAGNOSTIC ONLY, read-only. Samples best-offer-badged (`bestOfferAccepted: true`) and
plain Buy It Now sales from the local sold-comps.com raw capture (BUI-614), then calls
eBay's Trading API GetItem (free, user token) on each and compares `soldPrice` with
eBay's own `StartPrice` (the asking price) and `BestOfferCount`. GetItem answers for
items that ended within about 90 days.

    uv run --project apps/ebay python docs/audit/2026-10-11-best-offer-list-price.py
"""

import glob
import gzip
import json
import random
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "ebay" / "src"))
import ebay_user_token as eut  # noqa: E402

CAPTURE = Path.home() / ".local" / "share" / "ebay-sold-comps-capture"
SINCE = "2026-07-20"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}


def load_items():
    items = {}
    for f in glob.glob(str(CAPTURE / "raw_responses*")):
        op = gzip.open if f.endswith(".gz") else open
        with op(f, "rt") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for it in (rec.get("response") or {}).get("items") or []:
                    if it.get("itemId"):
                        items[it["itemId"]] = it
    return items


def get_item(tok, iid):
    body = ('<?xml version="1.0" encoding="utf-8"?><GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<ItemID>{iid}</ItemID><DetailLevel>ReturnAll</DetailLevel></GetItemRequest>")
    r = requests.post("https://api.ebay.com/ws/api.dll", data=body.encode(), headers={
        "X-EBAY-API-CALL-NAME": "GetItem", "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1349", "X-EBAY-API-IAF-TOKEN": tok,
        "Content-Type": "text/xml"}, timeout=60)
    root = ET.fromstring(r.content)
    return lambda path: root.findtext(path, namespaces=NS)


def main():
    items = load_items()
    recent = [i for i in items.values()
              if (i.get("endedAt") or "") >= SINCE and i.get("soldCurrency") == "USD"]
    obo = [i for i in recent if i.get("bestOfferAccepted") is True]
    bin_ = [i for i in recent if i.get("bestOfferAccepted") is False and i.get("buyingFormat") == "buyItNow"]
    random.seed(1222)
    pick = random.sample(obo, 25) + random.sample(bin_, 8)
    tok = eut.get_user_access_token()
    equal = {True: 0, False: 0}
    total = {True: 0, False: 0}
    for p in pick:
        g = get_item(tok, p["itemId"])
        start = g("e:Item/e:StartPrice")
        same = start is not None and abs(float(p["soldPrice"]) - float(start)) < 0.005
        total[p["bestOfferAccepted"]] += 1
        equal[p["bestOfferAccepted"]] += same
        print(p["itemId"], p["bestOfferAccepted"], p["soldPrice"], start,
              g("e:Item/e:BestOfferDetails/e:BestOfferCount"), p["title"][:50])
        time.sleep(0.3)
    for k in (True, False):
        print(f"bestOfferAccepted={k}: soldPrice == StartPrice on {equal[k]}/{total[k]}")


if __name__ == "__main__":
    main()
