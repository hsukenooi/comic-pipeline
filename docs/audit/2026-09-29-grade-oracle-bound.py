# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Measure the oracle bound of grading ungraded raw comps (BUI-1014).

DIAGNOSTIC ONLY. Opens ~/.comics-server/db.sqlite with mode=ro; nothing is
written to it and no provider is called.

The question: how much of the FMV pain (refusals, wide bands) on raw comps is
caused by comps that carry no grade, as opposed to sales that do not exist?
`fmv_math.build_pool` drops every grade-less comp, so an ungraded comp is
invisible to a grade-windowed pool today.

Subcommands:

  where   Direction 2. Where the ungraded raw comps sit: era, price level,
          sold-date recency (the 90-day photo window), top books, and whether
          they land on slab-watch, wish-listed, or bid-on books. Also the
          same-book graded mix, to judge whether blanking graded comps is a
          fair proxy for the ungraded ones.

  bound   Direction 1. On BUI-1005's leave-one-out population (graded raw
          ledger sales with a same-book comp within +/-2.0 in the prior 90
          days), blank the grade on a random share of the TRAINING comps equal
          to the observed ungraded rate, then price every held-out sale three
          ways with the unchanged `compute_fmv`:
            full       every graded comp keeps its grade (the oracle)
            blanked    the blanked comps are passed with grade=None (exactly
                       what an ungraded comp is today: build_pool drops it)
            select40   grades restored on a random 40% of the blanked comps
                       (the coverage a selective grader reaches)
          Two blanking designs: `strat` (the ticket's: per era x comp-price
          stratum at that stratum's ungraded rate) and `book` (each book at its
          OWN ungraded rate, which keeps the per-pool loss realistic).
          Reports refusals resolved and Winkler/price deltas with
          book-clustered bootstrap CIs, by training-pool price level, over
          several seeds.

  ceiling Real data, no blanking. For the harness population plus the sales it
          excludes for having no graded comp: how many of today's refused
          sales have any ungraded same-book comp in their 90-day window (the
          hard ceiling: a sale with none cannot be rescued by any grader), and
          two no-grader imputations from the book's graded comps sold before
          the sale: price-matched (one of the three nearest in log price) and
          random. Donors never include the sale or anything after it.

  curve   The `bound` proxy runs at 39% of today's graded density vs 100%;
          grading the real ungraded comps is 100% vs 257%. Measures the same
          x2.57 step from several base densities to show the direction of
          that bias.

All reads are pinned to rows first seen before AS_OF.

    uv run --python 3.12 docs/audit/2026-09-29-grade-oracle-bound.py where
    uv run --python 3.12 docs/audit/2026-09-29-grade-oracle-bound.py bound --seeds 10
    uv run --python 3.12 docs/audit/2026-09-29-grade-oracle-bound.py curve --seeds 5
    uv run --python 3.12 docs/audit/2026-09-29-grade-oracle-bound.py ceiling

The harness (`2026-09-28-grade-adjusted-pool-backtest.py`) is imported, not
copied: load_comps, training, price_current's compute_fmv call, metrics,
cluster_bootstrap_gap, era are all its own.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import random
import re
import sqlite3
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "gixen-overlay" / "src"))
_spec = importlib.util.spec_from_file_location(
    "bt1005", ROOT / "docs" / "audit" / "2026-09-28-grade-adjusted-pool-backtest.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)  # also puts apps/fmv/src on sys.path
fmv_math = H.fmv_math

TODAY = date(2026, 9, 29)
# The ledger grows while collection jobs run; pin every read to the rows first
# seen before this instant so the numbers reproduce.
AS_OF = "2026-09-29T09:00:00"
PHOTO_WINDOW = 90
SELECT_SHARE = 0.40
# ceiling imputation arms: price-matched (optimistic: grade follows price) and
# random donor grade (pessimistic: grade independent of price)
IMP_ARMS = ("imp_price", "imp_random")
PRICE_CUTS = (("< $20", 0, 20), ("$20-$100", 20, 100), (">= $100", 100, math.inf))


def price_level(p: float) -> str:
    for name, lo, hi in PRICE_CUTS:
        if lo <= p < hi:
            return name
    raise ValueError(p)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{H.DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_graded(conn: sqlite3.Connection) -> tuple[list[dict], dict]:
    """The harness's load_comps, unchanged, then pinned to AS_OF. Dedup keeps
    the lowest id, so dropping later rows afterwards equals dropping them first."""
    comps, stats = H.load_comps(conn)
    comps = [c for c in comps if (c["first_seen_at"] or "") < AS_OF]
    stats = {**stats, "kept_as_of": len(comps)}
    return comps, stats


# ─── Ungraded loader (the harness's filters and dedup, grade IS NULL) ──────

def load_ungraded(conn: sqlite3.Connection) -> tuple[list[dict], dict]:
    rows = conn.execute(
        f"""
        SELECT c.id, c.comic_id, c.provider, c.product_id, c.price, c.sold_date,
               c.tier, k.title AS book, k.issue, k.year, k.variant
        FROM comps c JOIN comics k ON k.id = c.comic_id
        WHERE c.pool = 'raw' AND c.grade IS NULL AND c.excluded_code IS NULL
          AND c.price > 0 AND c.first_seen_at < ?
          AND COALESCE(c.tier, '') NOT IN ({",".join("?" * len(H.EXCLUDED_TIERS))})
        ORDER BY c.id
        """, (AS_OF, *H.EXCLUDED_TIERS)).fetchall()
    stats = {"loaded": len(rows), "no_date": 0, "dup_product": 0, "dup_value": 0}
    seen_pid: set = set()
    seen_val: set = set()
    out = []
    for r in rows:
        d = fmv_math._parse_sold_date(r["sold_date"])
        if d is None:
            stats["no_date"] += 1
            continue
        if r["product_id"]:
            k = (r["comic_id"], str(r["product_id"]))
            if k in seen_pid:
                stats["dup_product"] += 1
                continue
            seen_pid.add(k)
        kv = (r["comic_id"], round(r["price"], 2), d)
        if kv in seen_val:
            stats["dup_value"] += 1
            continue
        seen_val.add(kv)
        out.append({**dict(r), "grade": None, "d": d, "sold_date": d.isoformat()})
    stats["kept"] = len(out)
    return out, stats


def ledger_counts(conn: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        """SELECT pool, certifier != 'none' AS slab_cert,
                  SUM(grade IS NULL OR grade = '') AS ungraded, COUNT(*) AS n
           FROM comps WHERE excluded_code IS NULL AND first_seen_at < ?
           GROUP BY 1, 2 ORDER BY 1, 2""", (AS_OF,))]


# ─── Direction 2: where the ungraded sit ────────────────────────────────────

def _server_json(path: str):
    url = os.environ.get("COMICS_SERVER_URL") or os.environ.get("GIXEN_SERVER_URL")
    if not url:
        return None
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=20) as fh:
            return json.load(fh)
    except Exception as e:  # noqa: BLE001 - diagnostic: report and continue
        print(f"warning: {path} read failed: {e}", file=sys.stderr)
        return None


def _norm(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(vol\.? *\d+\)|\(\d{4}\)", " ", s)
    s = re.sub(r"^(the|a|an) ", "", s.strip())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def watch_ids() -> set[int]:
    """The slab watch set's comic_ids (`comic-fmv --list-slab-watch`'s
    source). Its books have raw comps too; empty when the server is down."""
    w = _server_json("/api/comics/slab-watch")
    return {it["comic_id"] for it in (w or {}).get("items", [])}


def wish_comic_ids(conn: sqlite3.Connection, wish: list[dict]) -> set[int]:
    """Approximate: comics rows whose normalized title + issue match a
    wish-list entry's series name + issue (wish entries carry no comic_id)."""
    keys = set()
    for w in wish:
        name = w.get("name") or ""
        if " #" not in name:
            continue
        issue = name.rsplit(" #", 1)[1].strip().lower()
        keys.add((_norm(w.get("series_name") or name.rsplit(" #", 1)[0]), issue))
    return {r["id"] for r in conn.execute("SELECT id, title, issue FROM comics")
            if (_norm(r["title"]), str(r["issue"]).strip().lower()) in keys}


def cmd_where(args) -> dict:
    conn = connect()
    graded, gstats = load_graded(conn)
    ung, ustats = load_ungraded(conn)
    out: dict = {"ledger": ledger_counts(conn), "graded_stats": gstats,
                 "ungraded_stats": ustats}
    print("ledger (pool, certifier present, ungraded, n):", out["ledger"])
    print(f"as of {AS_OF}; harness-filtered raw: graded {len(graded)}, ungraded {len(ung)}, "
          f"ungraded rate {len(ung) / (len(graded) + len(ung)):.1%}")

    def mix(cs, keyf):
        c = Counter(keyf(x) for x in cs)
        return {k: c[k] for k in sorted(c, key=str)}
    for name, keyf in (("era", lambda c: H.era(c["year"])),
                       ("price", lambda c: price_level(c["price"]))):
        mg, mu = mix(graded, keyf), mix(ung, keyf)
        print(f"\n| {name} | graded | ungraded | ungraded rate |\n|---|---|---|---|")
        for k in sorted(set(mg) | set(mu), key=str):
            g, u = mg.get(k, 0), mu.get(k, 0)
            print(f"| {k} | {g} | {u} | {u / (g + u):.1%} |")
        out[f"mix_{name}"] = {"graded": mg, "ungraded": mu}
    print(f"\nmedian comp price: graded ${median(c['price'] for c in graded):.2f}, "
          f"ungraded ${median(c['price'] for c in ung):.2f}")

    # Same-book comparison: on books holding both, is an ungraded comp cheaper?
    gb, ub = defaultdict(list), defaultdict(list)
    for c in graded:
        gb[c["comic_id"]].append(c["price"])
    for c in ung:
        ub[c["comic_id"]].append(c["price"])
    both = [k for k in ub if k in gb]
    rel = [math.log(median(ub[k]) / median(gb[k])) for k in both]
    print(f"books with ungraded comps: {len(ub)}; with graded too: {len(both)}; "
          f"ungraded comps on books with NO graded comp: "
          f"{sum(len(ub[k]) for k in ub if k not in gb)}")
    print(f"same-book median(ungraded)/median(graded): x{math.exp(median(rel)):.2f} "
          f"(books with both)")
    out["same_book_ratio"] = math.exp(median(rel))
    out["books_ung"], out["books_both"] = len(ub), len(both)

    recent = [c for c in ung if (TODAY - c["d"]).days <= PHOTO_WINDOW]
    print(f"ungraded sold within {PHOTO_WINDOW} days of {TODAY}: {len(recent)} "
          f"({len(recent) / len(ung):.1%})")
    out["recent"] = len(recent)

    top = Counter(c["comic_id"] for c in ung).most_common(15)
    meta = {c["comic_id"]: c for c in ung}
    print("\n| Book | Ungraded | Graded |\n|---|---|---|")
    for cid, n in top:
        m = meta[cid]
        print(f"| {m['book']} #{m['issue']} ({m['year']}) | {n} | {len(gb.get(cid, []))} |")
    out["top"] = [(meta[cid]["book"], meta[cid]["issue"], meta[cid]["year"], n,
                   len(gb.get(cid, []))) for cid, n in top]
    top10_share = sum(n for _, n in Counter(c["comic_id"] for c in ung).most_common(
        max(1, len(ub) // 10))) / len(ung)
    print(f"top 10% of books hold {top10_share:.1%} of ungraded comps")
    out["top10_share"] = top10_share

    watch = _server_json("/api/comics/slab-watch")
    wish = _server_json("/api/comics/wish-list")
    # bids.comic_id is unpopulated; a bid reaches its book via bid_fmvs or
    # the legacy bids.fmv_id (no FK), both through fmv.comic_id.
    bid_ids = {r[0] for r in conn.execute(
        """SELECT f.comic_id FROM bid_fmvs bf JOIN fmv f ON f.id = bf.fmv_id
           UNION SELECT f.comic_id FROM bids b JOIN fmv f ON f.id = b.fmv_id""")}
    # Live raw refusals: stored raw FMV rows with no band (the production pain).
    refused_ids = {r[0] for r in conn.execute(
        """SELECT DISTINCT comic_id FROM fmv WHERE certifier = 'none' AND high IS NULL
           AND flag_reason IS NOT NULL""")}
    sets = {"bid-on books": bid_ids, "live refused raw FMV rows": refused_ids}
    if watch is not None:
        sets["slab watch set"] = {it["comic_id"] for it in watch.get("items", [])}
    if wish is not None:
        sets["wish-listed (name match)"] = wish_comic_ids(conn, wish)
    for name, ids in sets.items():
        u = sum(1 for c in ung if c["comic_id"] in ids)
        u90 = sum(1 for c in recent if c["comic_id"] in ids)
        books = len({c["comic_id"] for c in ung if c["comic_id"] in ids})
        print(f"{name}: {len(ids)} books; ungraded comps on them {u} ({u / len(ung):.1%}), "
              f"on {books} books; within {PHOTO_WINDOW} days {u90}")
        out[f"on_{name}"] = {"books": len(ids), "ungraded": u, "ung_books": books, "recent": u90}
    conn.close()
    return out


# ─── Direction 1: blank graded comps, measure what their grades were worth ──

def population(comps: list[dict]) -> tuple[list[dict], dict[int, list[dict]]]:
    """BUI-1005's held-out sales, unchanged: a sale with >= 1 same-book comp
    within +/-2.0 in the prior 90 days (after the near-duplicate drop)."""
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    rows = []
    for c in comps:
        train, _ = H.training(by_book[c["comic_id"]], c)
        if not any(abs(t["grade"] - c["grade"]) <= fmv_math.MAX_GRADE_WINDOW for t in train):
            continue
        # Donor pool for the random-grade control: the book's graded comps
        # sold before the sale, never the sale or its provider copies.
        donors = [g for g in by_book[c["comic_id"]] if g["d"] < c["d"]
                  and not (g["grade"] == c["grade"] and abs(g["price"] - c["price"]) < 0.01
                           and (c["d"] - g["d"]).days <= 7)]
        rows.append({"target": c, "train": train, "comic_id": c["comic_id"], "donors": donors,
                     "price": c["price"], "era": H.era(c["year"]),
                     "train_median": median(t["price"] for t in train)})
    return rows, by_book


def price_with_blanks(train: list[dict], g: float, blank: set[int],
                      regrade: dict[int, float] | None = None) -> dict:
    """compute_fmv exactly as price_current calls it, except a blanked comp
    goes in with grade=None: build_pool drops it and only the informational
    ungraded anchor sees it, which is what happens to an ungraded comp today."""
    r = fmv_math.compute_fmv(
        [{"price": c["price"],
          "grade": (regrade or {}).get(c["id"]) if c["id"] in blank else c["grade"],
          "sold_date": c["sold_date"]} for c in train], g)
    return {"low": r["fmv_low"], "high": r["fmv_high"], "median": r["median"],
            "flag": r["flag_reason"], "n": r["n"]}


def priced(b: dict) -> bool:
    return b["high"] is not None and b["low"] is not None and b["high"] > 0


def outcome(b: dict) -> str:
    if priced(b):
        return "priced"
    return b["flag"] or "no_comps"


def blank_sets(comps: list[dict], ung: list[dict], rng: random.Random,
               design: str) -> tuple[set[int], set[int]]:
    """Returns (blanked ids, blanked ids still blank after the 40% restore)."""
    groups: dict = defaultdict(list)
    rate: dict = {}
    if design == "strat":
        key = lambda c: (H.era(c["year"]), price_level(c["price"]))  # noqa: E731
    else:
        key = lambda c: c["comic_id"]  # noqa: E731
    gcount, ucount = Counter(key(c) for c in comps), Counter(key(c) for c in ung)
    for k in gcount:
        rate[k] = ucount.get(k, 0) / (gcount[k] + ucount.get(k, 0))
    for c in comps:
        groups[key(c)].append(c["id"])
    blank, still = set(), set()
    for k, ids in sorted(groups.items(), key=lambda kv: str(kv[0])):
        ids = sorted(ids)
        m = round(rate[k] * len(ids))
        chosen = rng.sample(ids, m)
        blank.update(chosen)
        keep_blank = rng.sample(chosen, m - round(SELECT_SHARE * m))
        still.update(keep_blank)
    return blank, still


def wstats(rows: list[dict], key: str) -> list[tuple[int, float]]:
    return [(r["comic_id"], H.winkler_score(float(r[key]["low"]), float(r[key]["high"]),
                                            r["price"]) / r["price"])
            for r in rows if priced(r[key])]


def run_seed(pop: list[dict], blank: set[int], still: set[int], reps: int,
             seed: int) -> dict:
    rng = random.Random(seed * 7 + 1)
    for r in pop:
        g = r["target"]["grade"]
        r["blanked"] = price_with_blanks(r["train"], g, blank)
        r["select40"] = price_with_blanks(r["train"], g, still)
        # Control: the blanked comps get a RANDOM grade drawn from the book's
        # known-grade comps (donors not blanked). Same comp count as `full`,
        # no grade information: separates "more comps" from "right grades".
        known = [d["grade"] for d in r["donors"] if d["id"] not in blank]
        regrade = ({c["id"]: rng.choice(known) for c in r["train"] if c["id"] in blank}
                   if known else {})
        r["shuffled"] = price_with_blanks(r["train"], g, blank, regrade)
    res: dict = {}
    cuts = {"all": pop}
    for name, lo, hi in PRICE_CUTS:
        cuts[name] = [r for r in pop if lo <= r["train_median"] < hi]
    if any(r["watch"] for r in pop):
        cuts["slab-watch books"] = [r for r in pop if r["watch"]]
    for cut, rows in cuts.items():
        ref = [r for r in rows if not priced(r["blanked"])]
        c: dict = {"n": len(rows), "refused_blanked": len(ref),
                   "refused_full": sum(not priced(r["full"]) for r in rows),
                   "refused_select40": sum(not priced(r["select40"]) for r in rows)}
        for arm in ("full", "select40", "shuffled"):
            res_n = sum(priced(r[arm]) for r in ref)
            c[f"resolved_{arm}"] = res_n
            c[f"resolved_{arm}_share"] = res_n / len(ref) if ref else None
            # the other direction: sales the blanked arm prices that the arm refuses
            c[f"lost_{arm}"] = sum(priced(r["blanked"]) and not priced(r[arm]) for r in rows)
        resc = [r for r in ref if priced(r["full"])]
        c["w_rescue_full"] = median(v for _, v in wstats(resc, "full")) if resc else None
        acc = [r for r in rows if priced(r["blanked"])]
        c["w_accepted_blanked"] = median(v for _, v in wstats(acc, "blanked")) if acc else None
        c["refusal_mix_blanked"] = dict(Counter(outcome(r["blanked"]) for r in ref))
        c["refusal_mix_full"] = dict(Counter(outcome(r["full"]) for r in rows
                                             if not priced(r["full"])))
        # Winkler/price: (a) every sale each arm prices; (b) the same rows,
        # i.e. band quality on sales blanking did not refuse.
        for arm in ("full", "select40", "shuffled"):
            a, b = wstats(rows, "blanked"), wstats(rows, arm)
            if a and b:
                gap = H.cluster_bootstrap_gap(a, b, reps=reps, seed=seed)
                c[f"w_gap_{arm}_all"] = gap
            same = [r for r in rows if priced(r["blanked"]) and priced(r[arm])]
            a, b = wstats(same, "blanked"), wstats(same, arm)
            if a and b:
                c[f"w_gap_{arm}_same"] = H.cluster_bootstrap_gap(a, b, reps=reps, seed=seed)
                c[f"w_same_n_{arm}"] = len(same)
                c[f"w_same_{arm}"] = median(v for _, v in b)
                c["w_same_blanked_" + arm] = median(v for _, v in a)
        # value of grade INFORMATION: true grades vs random grades, same comps
        same = [r for r in rows if priced(r["full"]) and priced(r["shuffled"])]
        if same:
            c["w_gap_shuffled_vs_full"] = H.cluster_bootstrap_gap(
                wstats(same, "shuffled"), wstats(same, "full"), reps=reps, seed=seed)
        for arm in ("full", "blanked", "select40", "shuffled"):
            w = wstats(rows, arm)
            c[f"w_median_{arm}"] = median(v for _, v in w) if w else None
            c[f"priced_{arm}"] = len(w)
            m = H.metrics(rows, arm)
            c[f"width_{arm}"] = m.get("width_median")
        res[cut] = c
    return res


def cmd_bound(args) -> dict:
    conn = connect()
    comps, gstats = load_graded(conn)
    ung, ustats = load_ungraded(conn)
    conn.close()
    pop, _ = population(comps)
    watch = watch_ids()
    for r in pop:
        r["full"] = H.price_current(r["train"], r["target"]["grade"])
        r["watch"] = r["comic_id"] in watch
    flags = Counter(outcome(r["full"]) for r in pop)
    print(f"population N={len(pop)} (graded kept {len(comps)}, ungraded kept "
          f"{len(ung)}); full-arm outcomes {dict(flags)}")
    out: dict = {"N": len(pop), "full_outcomes": dict(flags), "designs": {}}
    for design in args.designs:
        per_seed = []
        for s in range(args.seeds):
            seed = 1014 + s
            rng = random.Random(seed)
            blank, still = blank_sets(comps, ung, rng, design)
            r = run_seed(pop, blank, still, args.reps, seed)
            r["_blank"], r["_still"] = len(blank), len(still)
            per_seed.append(r)
            a = r["all"]
            print(f"[{design} seed {seed}] blanked {len(blank)}/{len(comps)} "
                  f"({len(blank) / len(comps):.1%}); refused blanked {a['refused_blanked']} "
                  f"-> full resolves {a['resolved_full']} ({a['resolved_full_share']:.1%}), "
                  f"select40 {a['resolved_select40']} ({a['resolved_select40_share']:.1%}); "
                  f"W same-rows blanked-full {a['w_gap_full_same'][0]:+.3f} "
                  f"[{a['w_gap_full_same'][1]:+.3f}, {a['w_gap_full_same'][2]:+.3f}]; "
                  f"W all-priced {a['w_gap_full_all'][0]:+.3f}")
        out["designs"][design] = per_seed
        summarize(design, per_seed)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1, default=str)
    return out


def ci3(xs: list[dict], key: str) -> str:
    ci = [x[key] for x in xs if x.get(key)]
    if not ci:
        return "-"
    return (f"{median(c[0] for c in ci):+.3f} [{median(c[1] for c in ci):+.3f}, "
            f"{median(c[2] for c in ci):+.3f}]")


def summarize(design: str, per_seed: list[dict]) -> None:
    def rng_str(vals, pct=False):
        vals = [v for v in vals if v is not None]
        if not vals:
            return "-"
        f = (lambda v: f"{100 * v:.1f}%") if pct else (lambda v: f"{v:+.3f}")
        return f"{f(median(vals))} ({f(min(vals))} to {f(max(vals))})"
    print(f"\n## Design `{design}`: median over {len(per_seed)} seeds (min to max)\n")
    print("| Cut | Sales | Refused (blanked) | Resolved by full grades | Resolved by 40% restore "
          "| Resolved by random grades | Lost by full | W/price gap same rows (blanked - full) "
          "[seed-median CI] | W/price gap all priced | 40%: gap same rows "
          "| Random - true grades, same rows [CI] | W/price of full-grade rescues |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for cut in [k for k in per_seed[0] if not k.startswith("_")]:
        xs = [s[cut] for s in per_seed]
        ci = [x.get("w_gap_full_same") for x in xs]
        ci = [c for c in ci if c]
        ci_str = (f"{median(c[0] for c in ci):+.3f} [{median(c[1] for c in ci):+.3f}, "
                  f"{median(c[2] for c in ci):+.3f}]") if ci else "-"
        print(f"| {cut} | {xs[0]['n']} | {median(x['refused_blanked'] for x in xs):.0f} | "
              f"{rng_str([x['resolved_full_share'] for x in xs], True)} | "
              f"{rng_str([x['resolved_select40_share'] for x in xs], True)} | "
              f"{rng_str([x['resolved_shuffled_share'] for x in xs], True)} | "
              f"{median(x['lost_full'] for x in xs):.0f} | {ci_str} | "
              f"{rng_str([x.get('w_gap_full_all', [None])[0] for x in xs])} | "
              f"{rng_str([x.get('w_gap_select40_same', [None])[0] for x in xs])} | "
              f"{ci3(xs, 'w_gap_shuffled_vs_full')} | "
              f"{rng_str([x.get('w_rescue_full') for x in xs]).replace('+', '')} |")


# ─── Ceiling on real data ───────────────────────────────────────────────────

def cmd_ceiling(args) -> dict:
    conn = connect()
    comps, _ = load_graded(conn)
    ung, _ = load_ungraded(conn)
    conn.close()
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    ub: dict[int, list[dict]] = defaultdict(list)
    for c in ung:
        ub[c["comic_id"]].append(c)
    watch = watch_ids()
    rng = random.Random(1014)
    rows, udup = [], 0
    for c in comps:
        train, _ = H.training(by_book[c["comic_id"]], c)
        in_pop = any(abs(t["grade"] - c["grade"]) <= fmv_math.MAX_GRADE_WINDOW for t in train)
        lo = c["d"] - timedelta(days=H.WINDOW_DAYS)
        uw = []
        for u in ub.get(c["comic_id"], []):
            if not (lo <= u["d"] < c["d"]):
                continue
            # A grade-less row can be a provider copy of the held-out sale
            # itself: same price within 7 days (the harness's own rule).
            if abs(u["price"] - c["price"]) < 0.01 and (c["d"] - u["d"]).days <= 7:
                udup += 1
                continue
            uw.append(u)
        # Donors: the same book's graded comps sold strictly before the
        # held-out sale (any age), never the sale or its copies, so no grade or
        # price from the sale or from the future leaks into an imputed grade.
        donors = [g for g in by_book[c["comic_id"]] if g["d"] < c["d"]
                  and not (g["grade"] == c["grade"] and abs(g["price"] - c["price"]) < 0.01
                           and (c["d"] - g["d"]).days <= 7)]
        row = {"comic_id": c["comic_id"], "price": c["price"], "in_pop": in_pop,
               "n_ung": len(uw), "current": H.price_current(train, c["grade"]),
               "basis": median(t["price"] for t in train) if train else
               (median(u["price"] for u in uw) if uw else c["price"])}
        for arm in IMP_ARMS:
            extra = []
            if donors:
                for u in uw:
                    if arm == "imp_price":
                        near = sorted(donors, key=lambda g: abs(math.log(g["price"] / u["price"])))[:3]
                        gr = rng.choice(near)["grade"]
                    else:
                        gr = rng.choice(donors)["grade"]
                    extra.append({"price": u["price"], "grade": gr, "sold_date": u["sold_date"]})
            r = fmv_math.compute_fmv([H.as_comp(t) for t in train] + extra, c["grade"])
            row[arm] = {"low": r["fmv_low"], "high": r["fmv_high"], "median": r["median"],
                        "flag": r["flag_reason"]}
        rows.append(row)
    print(f"ungraded provider copies of a held-out sale dropped: {udup}")
    out: dict = {}
    groups = [("harness population", [r for r in rows if r["in_pop"]]),
              ("excluded (no graded comp within +/-2.0)", [r for r in rows if not r["in_pop"]])]
    if watch:
        groups.append(("slab-watch books, harness population",
                       [r for r in rows if r["in_pop"] and r["comic_id"] in watch]))
    for label, sub in groups:
        ref = [r for r in sub if not priced(r["current"])]
        has = [r for r in ref if r["n_ung"] >= 1]
        has3 = [r for r in ref if r["n_ung"] >= 3]
        o: dict = {"n": len(sub), "refused": len(ref), "refused_with_any_ungraded": len(has),
                   "refused_with_3plus_ungraded": len(has3),
                   "refusal_mix": dict(Counter(outcome(r["current"]) for r in ref))}
        print(f"\n{label}: {len(sub)} sales, {len(ref)} refused {o['refusal_mix']}")
        if ref:
            print(f"  refused with >=1 ungraded comp in window: {len(has)} "
                  f"({len(has) / len(ref):.1%}); >=3: {len(has3)} ({len(has3) / len(ref):.1%})")
        for arm in IMP_ARMS:
            res = [r for r in ref if priced(r[arm])]
            lost = [r for r in sub if priced(r["current"]) and not priced(r[arm])]
            o[arm] = {"resolves": len(res), "loses": len(lost)}
            if ref:
                print(f"  {arm}: prices {len(res)} ({len(res) / len(ref):.1%}) of the refused; "
                      f"newly refuses {len(lost)} it used to price")
            for name, plo, phi in PRICE_CUTS:
                pr = [r for r in ref if plo <= r["basis"] < phi]
                if pr:
                    k = sum(priced(r[arm]) for r in pr)
                    print(f"    {name}: {k}/{len(pr)} ({k / len(pr):.1%}) resolved")
                    o[arm][name] = (k, len(pr))
            both = [r for r in sub if priced(r["current"]) and priced(r[arm])]
            if len(both) >= 20:
                gap = H.cluster_bootstrap_gap(wstats(both, "current"), wstats(both, arm),
                                              reps=args.reps)
                print(f"    same rows ({len(both)}): W/price current - {arm} {gap[0]:+.3f} "
                      f"[{gap[1]:+.3f}, {gap[2]:+.3f}]")
                o[arm]["w_gap_same"] = gap
            if res:
                w = [v for _, v in wstats(res, arm)]
                o[arm]["w_resolved_median"] = median(w)
                print(f"    median W/price on its rescues: {median(w):.3f}")
        out[label] = o
    return out



# ─── Density curve: does the proxy's answer hold at the real density? ──────

def cmd_curve(args) -> dict:
    """The `bound` proxy compares a pool at 39% of today's graded density with
    one at 100%. Grading the real ungraded comps compares 100% with 257%. Pool
    guards are count-driven and nonlinear, so the share resolved by the SAME
    x2.57 step is measured at several base densities (nested random subsets of
    the graded comps, true grades throughout) to see which way it trends as
    the base rises toward today's 100%."""
    conn = connect()
    comps, _ = load_graded(conn)
    ung, _ = load_ungraded(conn)
    conn.close()
    ratio = (len(comps) + len(ung)) / len(comps)
    sel = 1 + SELECT_SHARE * (ratio - 1)
    pop, _ = population(comps)
    bases = [b for b in args.bases if b * ratio <= 1.0 + 1e-9]
    print(f"oracle ratio (graded + ungraded) / graded = {ratio:.3f}; 40% selective = x{sel:.3f}")
    print("\n| Base density | Refused at base | Resolved at x ratio (true grades) "
          "| Resolved at x 40% selective | W/price gap same rows (base - x ratio) |")
    print("|---|---|---|---|---|")
    out = {}
    for base in bases:
        acc: dict = defaultdict(list)
        for s_ in range(args.seeds):
            rng = random.Random(2014 + s_)
            rank = {c["id"]: rng.random() for c in comps}

            def keep_under(f):
                return {i for i, v in rank.items() if v >= f}  # blanked = rank >= f

            arms = {"base": keep_under(base), "oracle": keep_under(base * ratio),
                    "sel": keep_under(base * sel)}
            for r in pop:
                g = r["target"]["grade"]
                for a, blank in arms.items():
                    r["c_" + a] = price_with_blanks(r["train"], g, blank)
            ref = [r for r in pop if not priced(r["c_base"])]
            acc["refused"].append(len(ref) / len(pop))
            acc["oracle"].append(sum(priced(r["c_oracle"]) for r in ref) / len(ref))
            acc["sel"].append(sum(priced(r["c_sel"]) for r in ref) / len(ref))
            same = [r for r in pop if priced(r["c_base"]) and priced(r["c_oracle"])]
            acc["wgap"].append(median(v for _, v in wstats(same, "c_base"))
                               - median(v for _, v in wstats(same, "c_oracle")))
        m = {k: median(v) for k, v in acc.items()}
        out[base] = {k: (median(v), min(v), max(v)) for k, v in acc.items()}
        print(f"| {base:.2f} (to {base * ratio:.2f}) | {100 * m['refused']:.1f}% | "
              f"{100 * m['oracle']:.1f}% ({100 * min(acc['oracle']):.1f}-{100 * max(acc['oracle']):.1f}) | "
              f"{100 * m['sel']:.1f}% ({100 * min(acc['sel']):.1f}-{100 * max(acc['sel']):.1f}) | "
              f"{m['wgap']:+.3f} ({min(acc['wgap']):+.3f} to {max(acc['wgap']):+.3f}) |")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("where")
    b = sub.add_parser("bound")
    b.add_argument("--seeds", type=int, default=5)
    b.add_argument("--reps", type=int, default=1000)
    b.add_argument("--designs", nargs="+", default=["strat", "book"],
                   choices=["strat", "book"])
    b.add_argument("--json")
    c = sub.add_parser("ceiling")
    c.add_argument("--reps", type=int, default=1000)
    cv = sub.add_parser("curve")
    cv.add_argument("--seeds", type=int, default=5)
    cv.add_argument("--bases", type=float, nargs="+",
                    default=[0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.3897])
    args = p.parse_args()
    {"where": cmd_where, "bound": cmd_bound, "ceiling": cmd_ceiling,
     "curve": cmd_curve}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
