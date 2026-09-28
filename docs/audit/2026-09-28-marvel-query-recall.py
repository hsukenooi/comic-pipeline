"""BUI-1004: measure two Marvel sold-comps query changes on a real sample.

Variants (all run through the real `fetch_book_comps` tier ladder):
  A  current: qualifier "marvel comics", broader tier when len(comps) < 5
  B  qualifier "marvel"
  C  broader tier when GRADED comps < 5 (THIN_RESULTS_THRESHOLD, which equals
     fmv_math.MIN_NARROW_POOL, the pool size build_pool widens toward)
  D  B + C

Usage (from the repo root; needs apps/ebay/.env for the provider keys):
  uv run --project apps/ebay python docs/audit/2026-09-28-marvel-query-recall.py run A OUTDIR
  ... run B / C / D ...
  uv run --project apps/ebay python docs/audit/2026-09-28-marvel-query-recall.py analyze OUTDIR

Side effects: provider calls and the ebay-sold-comps response cache only.
Never writes the comps ledger or the fmv table.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "ebay" / "src"))
sys.path.insert(0, str(ROOT / "apps" / "fmv" / "src"))

import sold_comps  # noqa: E402

BATCH = Path(__file__).with_suffix("").with_name(
    "2026-09-28-marvel-query-recall.batch.json")


def _gate_len(comps, year):
    return len(comps) < sold_comps.THIN_RESULTS_THRESHOLD and bool(year)


def _gate_graded(comps, year):
    graded = sum(1 for c in comps if c["grade"] is not None)
    return graded < sold_comps.THIN_RESULTS_THRESHOLD and bool(year)


VARIANTS = {
    "A": ("marvel comics", _gate_len),
    "B": ("marvel", _gate_len),
    "C": ("marvel comics", _gate_graded),
    "D": ("marvel", _gate_graded),
}


def run(variant: str, outdir: Path) -> None:
    qual, gate = VARIANTS[variant]
    sold_comps._MARVEL_QUALIFIER = qual
    sold_comps._should_broaden = gate
    books = json.loads(BATCH.read_text())
    inputs = [{k: v for k, v in b.items() if k not in ("cohort", "batch_unpriced")}
              for b in books]
    key = sold_comps.load_serpapi_key()
    results = sold_comps.run_batch(inputs, key, max_workers=4)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{variant}.json").write_text(json.dumps(results, indent=1))
    live = sum(1 for r in results for q in r["queries_used"] if not q.get("cached"))
    errs = sum(1 for r in results if r.get("error") or r.get("breaker_tripped"))
    print(f"{variant}: live provider calls={live} errors/breaker={errs}")


def _priceable(comps, target):
    """Approximate the comic-fmv guard: >=3 graded within +/-2.0, bracketing."""
    from fmv_math import build_pool, _classify_pool
    pool, _w = build_pool(comps, target)
    grades = [c["grade"] for c in pool]
    task_rule = len(pool) >= 3 and min(grades) <= target <= max(grades)
    flag, _span = _classify_pool(pool, target, len(pool))
    clean = bool(pool) and len(pool) >= 2 and flag is None
    return task_rule, clean, len(pool)


def analyze(outdir: Path) -> None:
    books = json.loads(BATCH.read_text())
    res = {v: json.loads((outdir / f"{v}.json").read_text()) for v in VARIANTS}
    rows = []
    for i, b in enumerate(books):
        row = {"book": f"{b['title']} #{b['issue']} ({b['year']})",
               "cohort": b["cohort"], "unpriced": b["batch_unpriced"],
               "grade": b["grade"]}
        base_ids = {c["product_id"] for c in res["A"][i]["comps"]}
        for v in VARIANTS:
            r = res[v][i]
            comps = r["comps"]
            added = [c for c in comps if c["product_id"] not in base_ids]
            row[v] = {
                "n": len(comps),
                "graded": sum(1 for c in comps if c["grade"] is not None),
                "tiers": sorted({q["tier"] for q in r["queries_used"]}),
                "live": sum(1 for q in r["queries_used"] if not q.get("cached")),
                "added": [(c["grade"], c["price"], c["title"]) for c in added],
                "priceable": _priceable(comps, b["grade"]),
                "error": r.get("error"),
            }
        rows.append(row)
    (outdir / "analysis.json").write_text(json.dumps(rows, indent=1))
    for row in rows:
        cells = " ".join(
            f"{v}:{row[v]['n']}/{row[v]['graded']}"
            f"{'*' if row[v]['priceable'][0] else ''}"
            f"(+{len(row[v]['added'])},{row[v]['live']}L)"
            for v in VARIANTS)
        print(f"{row['cohort'][:3]} {row['book']:<40} {cells}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        run(sys.argv[2], Path(sys.argv[3]))
    else:
        analyze(Path(sys.argv[2]))
