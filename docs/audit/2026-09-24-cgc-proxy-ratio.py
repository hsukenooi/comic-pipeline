"""BUI-976: raw/slab price ratio from the comps ledger (read-only).

Run from the repo root:  python3 docs/audit/2026-09-24-cgc-proxy-ratio.py [GRADE_TOL] [MIN_SIDE] [-v]
  GRADE_TOL 0 = exact grade pairing, 0.5 = +/-0.5 sensitivity
  MIN_SIDE  minimum sales on EACH side of a (book, grade) cell
"""
import os, re, sqlite3, statistics as st, sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.environ.get("EBAY_SRC", "apps/ebay/src"))
import comic_identity as ci  # production reject chain (apps/ebay/src)

DB = f"file:{os.path.expanduser('~/.comics-server/db.sqlite')}?mode=ro"
WINDOW_START = datetime(2026, 3, 1)  # both pools: sales on/after this date
args = [a for a in sys.argv[1:] if not a.startswith("-")]
GRADE_TOL = float(args[0]) if args else 0.0
MIN_SIDE = int(args[1]) if len(args) > 1 else 1
VERBOSE = "-v" in sys.argv
# Wrong-book / not-a-single-raw-copy markers the production chain misses
# (measured on this ledger: modern same-number issues, spin-off volumes, facsimiles).
MARKERS = re.compile(
    r"\b(cgc|cbcs|pgx|slab(bed)?|graded|lot|set|facsimil\w*|reprint|replica|reproduction|"
    r"variant|virgin|exclusive|incentive|homage|milestone|ultimate|astonishing|"
    r"all[- ]new|wolverine|annual|special|marvel legacy|true believers|"
    r"\d+(st|nd|rd|th) print(ing)?|second print(ing)?|\d:\d+)\b|#\s*\d+[a-z]\b", re.I)
YEAR = re.compile(r"\b(19[3-9]\d|20[0-2]\d)\b")

def pd(s):
    try: return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError: return datetime.strptime(s, "%b %d, %Y")

con = sqlite3.connect(DB, uri=True)
meta = {i: (t, str(n), y) for i, t, n, y in con.execute("select id,title,issue,year from comics")}
rows = con.execute("""select pool, comic_id, price, grade, sold_date, title, certifier, label
                      from comps where excluded_code is null and comic_id is not null
                      and price > 0""").fetchall()
# book year: comics.year, else the modal year printed in its slab titles
slab_years = defaultdict(Counter)
for pool, cid, *_r, title, _c, _l in rows:
    if pool == "slab":
        for y in YEAR.findall(title or ""): slab_years[cid][int(y)] += 1
def book_year(cid):
    y = meta[cid][2]
    return y or (slab_years[cid].most_common(1)[0][0] if slab_years[cid] else None)

def raw_reject(cid, title):
    series, issue, _ = meta[cid]
    by = book_year(cid)
    if MARKERS.search(title) or ci.is_comp_excluded(title): return "marker"
    if ci.should_reject(title, series, issue, release_year=str(by) if by else None): return "chain"
    if by and any(abs(int(y) - by) > 2 for y in YEAR.findall(title)): return "year"
    return None

raw, slab, ungraded = defaultdict(list), defaultdict(list), defaultdict(list)
dropped = Counter()
seen = set()
for pool, cid, price, grade, sd, title, cert, label in rows:
    # the same sale is stored once per provider (serpapi "Jul 5, 2026" / sold-comps.com "2026-07-05")
    key = (pool, cid, grade, round(price, 2), pd(sd).date())
    if key in seen:
        dropped[f"{pool}_duplicate_sale"] += 1; continue
    seen.add(key)
    if pd(sd) < WINDOW_START:
        dropped[f"{pool}_out_of_window"] += 1; continue
    if pool == "slab":
        if label != "universal" or cert not in ("cgc", "cbcs") or grade is None:
            dropped["slab_nonuniversal"] += 1; continue
        by = book_year(cid)  # same wrong-volume year test as raw (e.g. Marvel Feature #1 1971 vs 1975)
        if (by and any(abs(int(y) - by) > 2 for y in YEAR.findall(title or ""))) \
                or re.search(r"facsimil|reprint", title or "", re.I):
            dropped["slab_year_or_reprint"] += 1; continue
        slab[(cid, grade)].append(price)
    else:
        why = raw_reject(cid, title or "")
        if why:
            if cid in {c for c, _ in slab}: dropped[f"raw_{why}"] += 1
            continue
        (ungraded[cid] if grade is None else raw[(cid, grade)]).append(price)

cells = []
for (cid, g), sp in slab.items():
    rp = [p for (c, rg), ps in raw.items() if c == cid and abs(rg - g) <= GRADE_TOL + 1e-9 for p in ps]
    if len(rp) < MIN_SIDE or len(sp) < MIN_SIDE: continue
    s, r = st.median(sp), st.median(rp)
    ug = ungraded.get(cid, [])
    cells.append(dict(cid=cid, g=g, s=s, r=r, ratio=r / s, ns=len(sp), nr=len(rp), year=book_year(cid),
                      anchor=st.median(ug) if len(ug) >= 8 else None))

def summ(label, cs):
    if not cs: return
    q = sorted(c["ratio"] for c in cs); n = len(q)
    pct = lambda p: q[min(n - 1, int(p * (n - 1) + 0.5))]
    lo = sum(c["ratio"] < 0.50 for c in cs); hi = sum(c["ratio"] > 0.55 for c in cs)
    over = sum(c["ratio"] < 0.385 for c in cs)  # proxy max_bid (0.70 x 0.55 x slab) above raw median
    tag = "" if n >= 3 else "  (n<3: not a finding)"
    print(f"| {label} | {n} | {len({c['cid'] for c in cs})} | {st.median(q):.2f} | {pct(.25):.2f}-{pct(.75):.2f} "
          f"| {lo} | {n-lo-hi} | {hi} | {over} |{tag}")

print(f"GRADE_TOL={GRADE_TOL} MIN_SIDE={MIN_SIDE} window>={WINDOW_START.date()} dropped={dict(dropped)}")
print("| cut | cells | books | median | IQR | <0.50 | in band | >0.55 | ratio<0.385 |")
summ("all", cells)
for lo_, hi_ in [(0, 100), (100, 400), (400, 1000), (1000, 1e9)]:
    summ(f"slab ${lo_:.0f}-{hi_:.0f}", [c for c in cells if lo_ <= c["s"] < hi_])
summ("slab >= $400 (proxy floor)", [c for c in cells if c["s"] >= 400])
for lo_, hi_ in [(0, 4), (4, 6), (6, 8), (8, 10.1)]:
    summ(f"grade {lo_}-{hi_}", [c for c in cells if lo_ <= c["g"] < hi_])
for lo_, hi_ in [(0, 1970), (1970, 1985), (1985, 3000)]:
    summ(f"year {lo_}-{hi_ - 1}", [c for c in cells if c["year"] and lo_ <= c["year"] < hi_])
summ("ns>=2 & nr>=2", [c for c in cells if c["ns"] >= 2 and c["nr"] >= 2])
summ("ns>=3 & nr>=3", [c for c in cells if c["ns"] >= 3 and c["nr"] >= 3])
# per-book medians (a book with many grades must not dominate)
bk = defaultdict(list)
for c in cells: bk[c["cid"]].append(c["ratio"])
bm = sorted(st.median(v) for v in bk.values())
print(f"per-book median ratio: books={len(bm)} median={st.median(bm):.2f} "
      f"IQR={bm[len(bm)//4]:.2f}-{bm[3*len(bm)//4]:.2f}")
# cgc_cross_check: implied_raw = 0.525 x slab vs raw median, DIVERGES if |diff|/raw > 0.40
div = [c for c in cells if abs(0.525 * c["s"] - c["r"]) / c["r"] > 0.40]
print(f"cross_check DIVERGES {len(div)}/{len(cells)} (ratio>0.875: {sum(c['ratio'] > 0.875 for c in div)}, "
      f"ratio<0.375: {sum(c['ratio'] < 0.375 for c in div)})")
wa = [c for c in cells if c["anchor"]]
fires = [c for c in wa if 0.50 * c["s"] > 1.5 * c["anchor"] or 0.55 * c["s"] < 0.5 * c["anchor"]]
below = [c for c in wa if 0.55 * c["s"] < c["anchor"]]
print(f"anchor (grade-less raw median, n>=8) on {len(wa)} cells: anchor_diverges(T=0.5) fires {len(fires)} "
      f"(band above {sum(0.50*c['s'] > 1.5*c['anchor'] for c in fires)}, below {sum(0.55*c['s'] < 0.5*c['anchor'] for c in fires)}); "
      f"band top below anchor {len(below)}")
# proxy-floor subset (slab >= CGC_PROXY_MIN_SLAB_PRICE): candidate bands and guards
P = [c for c in cells if c["s"] >= 400]
if P:
    for lo_, hi_ in [(0, 6), (6, 8), (8, 10.1)]:
        summ(f"proxy floor, grade {lo_}-{hi_}", [c for c in P if lo_ <= c["g"] < hi_])
    print("| band | ratio<LOW | ratio<0.7*HIGH (max bid > raw median) | ratio>HIGH | max bid < raw median |")
    for lo_, hi_ in [(0.50, 0.55), (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.70, 0.80)]:
        b = sum(c["ratio"] < 0.7 * hi_ for c in P)
        print(f"| {lo_:.2f}-{hi_:.2f} | {sum(c['ratio'] < lo_ for c in P)} | {b} | "
              f"{sum(c['ratio'] > hi_ for c in P)} | {len(P) - b} |  (of {len(P)})")
    A = [c for c in P if c["anchor"]]
    for k in (1.0, 0.8, 0.5):
        fire = [c for c in A if 0.55 * c["s"] < k * c["anchor"]]
        print(f"guard band_top < {k} x anchor: fires {len(fire)}/{len(A)}, of which ratio>0.55: "
              f"{sum(c['ratio'] > 0.55 for c in fire)} (all ratio>0.55: {sum(c['ratio'] > 0.55 for c in A)})")
    ad = [c for c in A if 0.50 * c["s"] > 1.5 * c["anchor"] or 0.55 * c["s"] < 0.5 * c["anchor"]]
    print(f"proxy floor: anchor_diverges(T=0.5) fires {len(ad)}/{len(A)} "
          f"(above {sum(0.50 * c['s'] > 1.5 * c['anchor'] for c in ad)}); cross_check DIVERGES analogue "
          f"{sum(not (0.375 <= c['ratio'] <= 0.875) for c in P)}/{len(P)}")
if VERBOSE:
    for c in sorted(cells, key=lambda c: c["ratio"]):
        t, i, _ = meta[c["cid"]]
        print(f"  {c['ratio']:.2f} {t[:30]} #{i} ({c['year']}) id={c['cid']} g={c['g']} slab={c['s']:.2f}(n{c['ns']}) "
              f"raw={c['r']:.0f}(n{c['nr']}) anchor={c['anchor'] and round(c['anchor'])}")
