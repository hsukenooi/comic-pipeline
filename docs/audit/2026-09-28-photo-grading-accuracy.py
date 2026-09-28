#!/usr/bin/env python3
"""Measure photo-grading accuracy on labeled sold comps (BUI-1006).
DIAGNOSTIC ONLY. The DB is opened read-only; nothing is written to it.

Four steps, each a subcommand, all state in --workdir (scratch, not the repo):

  sample   Draw candidates from the comps ledger, newest sold_date first,
           stratified by grade band. Raw: pool='raw', seller-stated grade, no
           exclusion code, title not a lot/set/run and not a slab. Slab:
           pool='slab', label='universal', CGC/CBCS, grade 2.0-10.0.
  fetch    For each candidate (band by band, newest first) fetch the listing
           through the Browse API (ebay_fetch.fetch_item_with_status, the same
           call grade_photos.download_listing makes) and download ONLY the
           first image (the front cover). Stop a band when its quota is met.
           Downscale to <=768px long edge, JPEG q80. Slabs: crop the top
           SLAB_CROP of the frame (the certifier label band). Files are named
           by a random 8-hex id; the id -> comp key lives in key.json only.
           Every failure is recorded with its reason (failure rate = the
           doc's fetch-failure number).
  batches  Shuffle raw and slab images together and write batches of 10
           image paths (batches.json). Each batch is graded by one Haiku
           subagent through Claude Code's Agent tool (no API key exists here),
           with GRADER_PROMPT below. Each subagent's JSON lines are saved to
           grades/batch-NN.jsonl.
  metrics  Join grades to key.json and print the MAE / within +-1.0 /
           within +-0.5 / bias tables, the band breakdown, and the confidence
           table.

    uv run --project apps/ebay --with pillow python \\
        docs/audit/2026-09-28-photo-grading-accuracy.py <step> --workdir DIR

Grader prompt (the grading step; run once per batch as
Agent(subagent_type="general-purpose", model="haiku", prompt=GRADER_PROMPT
with {paths} filled in)):
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import sqlite3
import statistics
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

DB = os.path.expanduser("~/.comics-server/db.sqlite")
EBAY_SRC = Path(__file__).resolve().parents[2] / "apps" / "ebay" / "src"

RAW_BANDS = [(0.5, 3.5, 17), (4.0, 5.5, 17), (6.0, 7.5, 17), (8.0, 8.5, 17), (9.0, 9.2, 16), (9.4, 10.0, 16)]
SLAB_BANDS = [(2.0, 3.5, 8), (4.0, 5.5, 9), (6.0, 7.5, 9), (8.0, 8.5, 8), (9.0, 9.4, 8), (9.6, 10.0, 8)]
LONG_EDGE = 768
JPEG_Q = 80
SLAB_CROP = 0.27  # drop the top 27% of a slab photo: the certifier label band

LOT_RE = re.compile(
    r"\blots?\b|\bsets?\b|\bruns?\b|\bbundle|\bcollection\b|complete|"
    r"#\s*\d+\s*-\s*#?\s*\d+|\b\d+\s*(comics|books|issues)\b|\bx\s?\d\b|"
    r"#\d+\s*(&|and|,)\s*#?\d+|\bpick\b|\bchoose\b|\byou pick\b",
    re.I,
)
SLAB_RE = re.compile(r"\b(cgc|cbcs|pgx|slab|graded|egs)\b", re.I)

GRADER_PROMPT = """You are a raw-comic condition grader. Grade each comic image below on the \
CGC/Overstreet 0.5-10.0 scale, in 0.5 steps (9.2, 9.6, 9.8 are also allowed), from what \
you can see of the cover. Some books are inside a plastic case; grade the book, not the case, \
and ignore glare on the case.

Rubric: 9.4 NM - flat, glossy, sharp corners, only tiny flaws. 8.0 VF - minor wear, a small \
stress line or slightly blunted corner, good gloss. 6.0 FN - noticeable wear: spine stress \
lines, soft corners, minor creases, some gloss loss. 4.0 VG - worn and used: multiple creases, \
rounded corners, spine roll or small tears, but complete. 2.0 GD - heavy wear: big creases, \
tears, chips, stains, possibly a detached cover or missing piece.

The file names are random ids and carry no information; do not guess from them. Use the Read \
tool to view each image. Output ONLY one JSON line per image, nothing else:
{{"file": "<file name>", "grade": <number>, "confidence": "high|medium|low", "defects": "<short>"}}

Images:
{paths}"""


# ------------------------------------------------------------------ helpers

def parse_date(s: str | None) -> str:
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip()[:10] if fmt == "%Y-%m-%d" else s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def band_of(g: float, bands) -> int | None:
    for i, (lo, hi, _) in enumerate(bands):
        if lo - 1e-9 <= g <= hi + 1e-9:
            return i
    return None


# ------------------------------------------------------------------ sample

def cmd_sample(wd: Path) -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT product_id, title, grade, pool, sold_date, certifier, label, excluded_code "
        "FROM comps WHERE grade IS NOT NULL AND excluded_code IS NULL"
    ).fetchall()
    conn.close()
    seen: set[tuple[str, str]] = set()
    cands: dict[str, list[list[dict]]] = {"raw": [[] for _ in RAW_BANDS], "slab": [[] for _ in SLAB_BANDS]}
    for pid, title, grade, pool, sold, cert, label, _ in rows:
        title = title or ""
        if (pool, pid) in seen:
            continue
        if pool == "raw":
            if LOT_RE.search(title) or SLAB_RE.search(title):
                continue
            bands = RAW_BANDS
        else:
            if label != "universal" or cert not in ("cgc", "cbcs"):
                continue
            bands = SLAB_BANDS
        b = band_of(grade, bands)
        if b is None:
            continue
        seen.add((pool, pid))
        cands[pool][b].append({"product_id": pid, "title": title, "grade": grade, "pool": pool,
                               "sold_date": parse_date(sold), "band": b})
    for pool in cands:
        for lst in cands[pool]:
            lst.sort(key=lambda r: r["sold_date"], reverse=True)
    (wd / "candidates.json").write_text(json.dumps(cands, indent=1))
    for pool, bands in (("raw", RAW_BANDS), ("slab", SLAB_BANDS)):
        print(pool, [(f"{lo}-{hi}", len(cands[pool][i])) for i, (lo, hi, _) in enumerate(bands)])


# ------------------------------------------------------------------ fetch

def _prep_image(raw: bytes, pool: str) -> bytes:
    from PIL import Image

    im = Image.open(BytesIO(raw)).convert("RGB")
    if pool == "slab":
        w, h = im.size
        im = im.crop((0, int(h * SLAB_CROP), w, h))
    im.thumbnail((LONG_EDGE, LONG_EDGE))
    out = BytesIO()
    im.save(out, "JPEG", quality=JPEG_Q)
    return out.getvalue()


def cmd_fetch(wd: Path, only_pool: str | None) -> None:
    import requests

    sys.path.insert(0, str(EBAY_SRC))
    from ebay_fetch import fetch_item_with_status, get_token, load_config

    cands = json.loads((wd / "candidates.json").read_text())
    key_path, fail_path = wd / "key.json", wd / "failures.json"
    key = json.loads(key_path.read_text()) if key_path.exists() else {}
    failures = json.loads(fail_path.read_text()) if fail_path.exists() else []
    rejected = set(json.loads((wd / "rejected.json").read_text())) if (wd / "rejected.json").exists() else set()
    tried = {k["product_id"] for k in key.values()} | {f["product_id"] for f in failures} | rejected
    img_dir = wd / "images"
    img_dir.mkdir(exist_ok=True)
    cid, secret, base = load_config()
    token = get_token(cid, secret, base)
    for pool, bands in (("raw", RAW_BANDS), ("slab", SLAB_BANDS)):
        if only_pool and pool != only_pool:
            continue
        for b, (_, _, quota) in enumerate(bands):
            have = sum(1 for k in key.values() if k["pool"] == pool and k["band"] == b)
            for c in cands[pool][b]:
                if have >= quota:
                    break
                if c["product_id"] in tried:
                    continue
                tried.add(c["product_id"])
                data, status = fetch_item_with_status(c["product_id"], token, base, retries=1)
                if data is None and status == 401:
                    token = get_token(cid, secret, base, force_refresh=True)
                    data, status = fetch_item_with_status(c["product_id"], token, base, retries=1)
                reason = None
                url = ((data or {}).get("image") or {}).get("imageUrl")
                if data is None:
                    reason = f"browse_{status}"
                elif not url:
                    reason = "no_image"
                else:
                    try:
                        r = requests.get(url, timeout=30)
                        r.raise_for_status()
                        jpg = _prep_image(r.content, pool)
                    except Exception as e:  # noqa: BLE001 - record and move on
                        reason = f"image_{type(e).__name__}"
                if reason:
                    failures.append({**c, "reason": reason})
                    continue
                rid = secrets.token_hex(4)
                (img_dir / f"{rid}.jpg").write_bytes(jpg)
                key[rid] = c
                have += 1
            print(pool, b, "have", have, "of", quota, flush=True)
            key_path.write_text(json.dumps(key, indent=1))
            fail_path.write_text(json.dumps(failures, indent=1))
    n_ok, n_fail = len(key), len(failures)
    print(f"fetched {n_ok}, failed {n_fail} ({n_fail / max(1, n_ok + n_fail):.1%})")


def cmd_reject(wd: Path, rids: list[str]) -> None:
    """Drop images that failed the eyeball check (label survived crop, not a
    front cover). The product is added to rejected.json so fetch refills."""
    key = json.loads((wd / "key.json").read_text())
    rej_path = wd / "rejected.json"
    rej = json.loads(rej_path.read_text()) if rej_path.exists() else []
    for rid in rids:
        c = key.pop(rid)
        rej.append(c["product_id"])
        (wd / "images" / f"{rid}.jpg").unlink(missing_ok=True)
    (wd / "key.json").write_text(json.dumps(key, indent=1))
    rej_path.write_text(json.dumps(rej, indent=1))


# ------------------------------------------------------------------ batches

def cmd_batches(wd: Path, size: int) -> None:
    key = json.loads((wd / "key.json").read_text())
    rids = sorted(key)
    random.Random(1006).shuffle(rids)
    batches = [[str(wd / "images" / f"{r}.jpg") for r in rids[i:i + size]] for i in range(0, len(rids), size)]
    (wd / "batches.json").write_text(json.dumps(batches, indent=1))
    (wd / "grades").mkdir(exist_ok=True)
    print(f"{len(batches)} batches")


# ------------------------------------------------------------------ metrics

def _stats(pairs: list[tuple[float, float]]) -> str:
    if not pairs:
        return "| 0 | – | – | – | – |"
    err = [p - t for p, t in pairs]
    ae = [abs(e) for e in err]
    return (f"| {len(pairs)} | {statistics.mean(ae):.2f} | {sum(a <= 1.0 for a in ae) / len(ae):.0%} | "
            f"{sum(a <= 0.5 for a in ae) / len(ae):.0%} | {statistics.mean(err):+.2f} |")


def cmd_metrics(wd: Path) -> None:
    key = json.loads((wd / "key.json").read_text())
    preds: dict[str, dict] = {}
    for f in sorted((wd / "grades").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            d = json.loads(line)
            rid = Path(d["file"]).stem
            if rid in key:
                preds[rid] = d
    rows = [{**key[r], "pred": float(p["grade"]), "conf": p.get("confidence", "?")} for r, p in preds.items()]
    print(f"graded {len(rows)} of {len(key)}\n")
    hdr = "| Cut | n | MAE | within ±1.0 | within ±0.5 | bias (pred − true) |\n|---|---|---|---|---|---|"
    print(hdr)
    for name, sel in (("Raw", "raw"), ("Slab", "slab"), ("Combined", None)):
        pairs = [(r["pred"], r["grade"]) for r in rows if sel in (None, r["pool"])]
        print(f"| {name} " + _stats(pairs))
    for pool, bands in (("raw", RAW_BANDS), ("slab", SLAB_BANDS)):
        print(f"\n{pool} by true-grade band\n" + hdr.replace("Cut", "True grade"))
        for b, (lo, hi, _) in enumerate(bands):
            pairs = [(r["pred"], r["grade"]) for r in rows if r["pool"] == pool and r["band"] == b]
            print(f"| {lo}–{hi} " + _stats(pairs))
    for sel in ("raw", "slab", None):
        print(f"\nconfidence ({sel or 'combined'})\n" + hdr.replace("Cut", "Confidence"))
        for c in ("high", "medium", "low"):
            pairs = [(r["pred"], r["grade"]) for r in rows if r["conf"] == c and sel in (None, r["pool"])]
            print(f"| {c} " + _stats(pairs))
    # Context: correlation, a constant-guess baseline, and the raw result
    # reweighted to the ledger's natural grade mix (the sample is stratified).
    for sel in ("raw", "slab", None):
        sub = [r for r in rows if sel in (None, r["pool"])]
        p, t = [r["pred"] for r in sub], [r["grade"] for r in sub]
        med = statistics.median(t)
        base = sum(abs(med - x) <= 1.0 for x in t) / len(t)
        print(f"\n{sel or 'combined'}: pearson r={statistics.correlation(p, t):.2f}; "
              f"constant guess {med} -> within ±1.0 {base:.0%}, MAE {statistics.mean(abs(med - x) for x in t):.2f}")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    weights = []
    for lo, hi, _ in RAW_BANDS:
        (n,) = conn.execute(
            "SELECT COUNT(DISTINCT product_id) FROM comps WHERE pool='raw' AND excluded_code IS NULL "
            "AND grade BETWEEN ? AND ?", (lo - 1e-9, hi + 1e-9)).fetchone()
        weights.append(n)
    conn.close()
    hit = [statistics.mean(abs(r["pred"] - r["grade"]) <= 1.0 for r in rows if r["pool"] == "raw" and r["band"] == b)
           for b in range(len(RAW_BANDS))]
    print(f"raw within ±1.0 reweighted to ledger mix {weights}: "
          f"{sum(w * h for w, h in zip(weights, hit)) / sum(weights):.0%}")
    preds_all = [r["pred"] for r in rows]
    print("\npredicted-grade spread: min %.1f max %.1f stdev %.2f; true stdev %.2f" % (
        min(preds_all), max(preds_all), statistics.pstdev(preds_all), statistics.pstdev([r["grade"] for r in rows])))
    (wd / "joined.json").write_text(json.dumps(rows, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["sample", "fetch", "reject", "batches", "metrics"])
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--pool", choices=["raw", "slab"])
    ap.add_argument("--size", type=int, default=10)
    ap.add_argument("rids", nargs="*")
    a = ap.parse_args()
    a.workdir.mkdir(parents=True, exist_ok=True)
    if a.step == "sample":
        cmd_sample(a.workdir)
    elif a.step == "fetch":
        cmd_fetch(a.workdir, a.pool)
    elif a.step == "reject":
        cmd_reject(a.workdir, a.rids)
    elif a.step == "batches":
        cmd_batches(a.workdir, a.size)
    else:
        cmd_metrics(a.workdir)


if __name__ == "__main__":
    main()
