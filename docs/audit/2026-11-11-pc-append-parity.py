#!/usr/bin/env python3
"""BUI-1282: parity check of the PRODUCTION PriceCharting append + guard.
DIAGNOSTIC ONLY, read-only.

    uv run --project plugins/gixen-overlay python docs/audit/2026-11-11-pc-append-parity.py

Re-runs the "append with the refusal guard" arm of
`2026-10-18-slab-fmv-option-ranking.py` (doc: 0.516 [0.478, 0.565] on 860
priced sales) with the dedup and the guard taken from
`apps/fmv/src/pricecharting.py` instead of the audit's inline copies:

  * the PC rows a sale may see are the same (BUI-1229 test set, CGC universal,
    dated strictly before the sale, the sale and its `same_sale` twins hidden);
  * `pricecharting.append_rows` shapes them and drops ledger twins;
  * `b_guard` is the measured arm (b if a priced or b is `direct`, else None);
  * `b_prod` is production (`keep_appended`: b when it prices and a priced or
    b is `direct`, else a, so an appended refusal never unprices a row).

Also prints the bid side the doc left open: `max_bid` of b_prod vs a on rows
both price. Opens the ledger with mode=ro.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location(
    "option_ranking", HERE / "2026-10-18-slab-fmv-option-ranking.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
T = R.T
import pricecharting as PC  # noqa: E402  (apps/fmv/src is on sys.path via T)


def main() -> int:
    ts = T.load_test_set()
    cap = R.load_capture_flags()
    conn = sqlite3.connect(f"file:{T.DB}?mode=ro", uri=True)
    rows, bids = [], []
    dedup_mismatch = 0
    for b in ts["books"]:
        cid = b["comic_id"]
        ledger = R.load_ledger(conn, cid, cap)
        pcs = [{**s, "_date": date.fromisoformat(s["sold_date"])}
               for s in T.universal_sales(ts, cid)]
        for s in pcs:
            if not (R.WIN_FROM <= s["sold_date"] <= R.WIN_TO) or s["price_outlier"] is True:
                continue
            d, y, g, pq = s["_date"], s["price"], s["grade"], s["page_quality"]
            as_of = d - timedelta(days=1)
            led = [c for c in ledger if c["_date"] and c["_date"] < d and not T.same_sale(s, c)]
            pool_a = [{**c, "best_offer": bool(c["_bo"])} for c in led]
            prior = [p for p in pcs if p["_date"] < d and p["product_id"] != s["product_id"]
                     and not T.same_sale(s, p)]
            add = PC.append_rows(prior, pool_a, certifier="cgc", label="universal")
            audit_n = sum(not any(T.same_sale(p, c) for c in led) for p in prior)
            dedup_mismatch += audit_n != len(add)
            ra = R.price(pool_a, g, as_of, pq)
            rb = R.price(pool_a + add, g, as_of, pq)
            row = {"comic_id": cid, "a": R.score(ra, y), "b": R.score(rb, y)}
            row["b_guard"] = row["b"] if (row["a"] or (row["b"] and row["b"]["basis"] == "direct")) else None
            final = rb if PC.keep_appended(ra, rb) else ra
            row["b_prod"] = R.score(final, y)
            rows.append(row)
            if ra.get("max_bid") and final.get("max_bid") and final is rb:
                bids.append(final["max_bid"] / ra["max_bid"])
    print(f"rows={len(rows)} dedup_mismatch_vs_audit={dedup_mismatch}")
    for k in ("a", "b", "b_guard", "b_prod"):
        print(f"{k:8} {R.summarize(rows, k)}")
    for k in ("b_guard", "b_prod"):
        print(f"paired {k} vs a: {R.fmt_paired(R.paired(rows, k, 'a'))}")
    if bids:
        print(f"max_bid appended/ledger-only on {len(bids)} rows both price: "
              f"median {median(bids):.3f} mean {mean(bids):.3f} "
              f"lower {sum(x < 1 for x in bids)} higher {sum(x > 1 for x in bids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
