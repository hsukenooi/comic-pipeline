# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "torch",
#     "open_clip_torch",
#     "timm",
#     "pillow",
#     "requests",
#     "numpy",
#     "scikit-learn",
#     "opencv-python-headless",
#     "pyobjc-framework-Vision; sys_platform == 'darwin'",
# ]
# ///
"""Measure a multi-photo, native-resolution grader with selective coverage (BUI-1013).

DIAGNOSTIC ONLY. The comics DB is opened read-only; nothing is written to it.
The test set is the 150 BUI-1006 holdout comps, read from the BUI-1012 cache
(--holdout: manifest.json, full/<product_id>/img-NN.jpg, bui-1006/key.json,
bui-1006/joined.json, bui-1011/preds_*.json); the cache is never modified.
Every other file lives in --workdir.

It reuses BUI-1011's pieces by importing docs/audit/2026-09-28-embedding-grader.py
(ladder, bands, filters, dHash, encoders, weights, band-balanced metric, QWK,
table rows) and BUI-1012's listing fetcher from
docs/audit/2026-09-29-holdout-photo-cache.py (every image, native resolution,
item.json with legacyItemId + title for the listing-identity check).

Steps (subcommands), each resumable:

  sample   Training candidates as BUI-1011 cmd_sample (no excluded_code, the
           same title filters and slab rule, deduped on product_id, grade
           conflicts dropped), restricted to sold_date <= MAX_AGE_DAYS old (the
           Browse photo window), minus every holdout product_id, BUI-1006's
           rejected id, AND every comp sharing a comic_id with a holdout comp
           (the strict same-issue bound is the default here, not a variant).
  fetch    Browse API, every image per listing at native resolution, slabs
           first then raw newest-first, hard cap MAX_CALLS listings, 4 threads.
  prep     Per photo, train and holdout alike: slab photos lose the top 27%
           (BUI-1006's verified label crop); Apple Vision OCR then reads what is
           left. A photo whose text still carries a grade token, a certifier
           word or a grade adjective is DROPPED, except the front photo, whose
           offending text boxes are blacked out instead (the tiles need it).
           The front photo also gets a book-in-frame box (largest-rectangle
           heuristic, OpenCV edges + contours); the masked front is saved at
           native resolution. SHA-256 of the raw bytes and a dHash per photo.
  embed    Frozen encoder on MPS: every kept photo (224 px CLIP input), plus
           seven native-resolution tiles of the front photo (four corners, the
           spine strip in three segments), squashed to the encoder's input size.
  fit      Leakage filters (holdout product_id, raw-byte SHA-256 and dHash <= 6
           on any photo, front-photo cosine >= 0.95, front duplicates inside
           training; recurring boilerplate photos, a dHash seen in >= 3
           listings, are ignored everywhere first), then per ARM:
             front  front photo only (the BUI-1011 input on this training set)
             whole  mean + max pooled embeddings of every kept photo
             tiles  front tiles: 4 corners + mean of the 3 spine segments
             both   whole + tiles
           Grouped 5-fold StratifiedGroupKFold CV on TRAINING ONLY (groups =
           comic_id) picks the CORAL head's weight decay and class balancing by
           band-balanced within-1.0. The pick is refit on all training rows and
           scores the holdout once. Out-of-fold CV probabilities are kept for
           the calibration check. Writes preds_<arm>.json.
  metrics  BUI-1006's tables, paired against Haiku and BUI-1011, and the
           selective-coverage curve (confidence = CORAL probability mass within
           +/-1.0 of the predicted rung).
  sheet    Contact sheets for the manual crop and label-leak checks.

    uv run docs/audit/2026-09-29-multiphoto-grader.py <step> --workdir W --holdout H
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _mod(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


E = _mod("bui1011", "2026-09-28-embedding-grader.py")
C = _mod("bui1012", "2026-09-29-holdout-photo-cache.py")
LADDER, RAW_BANDS, SLAB_BANDS = E.LADDER, E.RAW_BANDS, E.SLAB_BANDS
band_of, rung, dhash, load, timing, parse_date, age_bucket = (
    E.band_of, E.rung, E.dhash, E.load, E.timing, E.parse_date, E.age_bucket)

DB = os.path.expanduser("~/.comics-server/db.sqlite")
MAX_AGE_DAYS = 90
MAX_CALLS = 2500
WORKERS = 4
SLAB_CROP = E.SLAB_CROP
ARMS = ("front", "whole", "tiles", "both")
TILE_NAMES = ("tl", "tr", "bl", "br", "sp0", "sp1", "sp2")
CORNER = 0.25   # corner tile side, as a fraction of the book box width
SPINE = 0.14    # spine strip width, as a fraction of the book box width
# Grade leakage in photo text: certifier / label words, a bare decimal grade
# token (3.0, 9.8, 10.0 but not 3.99 or 12), grade adjectives, and the
# abbreviations sellers write on bag cards. Case-insensitive except the
# abbreviations, which only count in capitals.
LEAK_WORDS = re.compile(
    r"\b(cgc|cbcs|pgx|egs|universal|grade[ds]?|graded|qualified|restored|signature series|"
    r"pages|near mint|very fine|very good|mint|fine|good|fair|poor)\b"
    r"|(?<![\d.$])(10\.0|\d\.\d)(?![\d])", re.I)
LEAK_ABBR = re.compile(r"\b(NM|VF|VG|FN|GD|FR|PR)\b[-+/]?")


# ------------------------------------------------------------------ sample

def cmd_sample(wd: Path, hd: Path) -> None:
    import sqlite3

    key = json.loads((hd / "bui-1006" / "key.json").read_text())
    banned = {v["product_id"] for v in key.values()} | set(load(hd / "bui-1006" / "rejected.json", []))
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT product_id, comic_id, title, grade, pool, sold_date, certifier, label "
        "FROM comps WHERE grade IS NOT NULL AND excluded_code IS NULL").fetchall()
    conn.close()
    hold_cids = sorted({cid for pid, cid, *_ in rows if pid in banned and cid is not None})
    ref = date.today()
    by_pid: dict[str, dict] = {}
    bad: set[str] = set()
    st = {"rows": len(rows), "holdout_or_rejected": 0, "filtered": 0, "grade_conflict": 0,
          "too_old": 0, "same_issue_as_holdout": 0}
    for pid, cid, title, grade, pool, sold, cert, label in rows:
        title = title or ""
        if pid in banned:
            st["holdout_or_rejected"] += 1
            continue
        if pool == "raw":
            if E.LOT_RE.search(title) or E.SLAB_RE.search(title):
                st["filtered"] += 1
                continue
        elif label != "universal" or cert not in ("cgc", "cbcs"):
            st["filtered"] += 1
            continue
        c = by_pid.get(pid)
        if c is None:
            by_pid[pid] = {"product_id": pid, "title": title, "grade": grade, "pool": pool,
                           "sold_date": parse_date(sold), "comic_ids": [cid] if cid is not None else []}
        elif c["grade"] != grade or c["pool"] != pool:
            bad.add(pid)
        else:
            if cid is not None and cid not in c["comic_ids"]:
                c["comic_ids"].append(cid)
            c["sold_date"] = max(c["sold_date"], parse_date(sold))
    for pid in bad:
        by_pid.pop(pid, None)
    st["grade_conflict"] = len(bad)
    hc = set(hold_cids)
    cands = {"raw": [], "slab": [], "same_issue": []}
    for c in by_pid.values():
        if not c["sold_date"] or (ref - date.fromisoformat(c["sold_date"])).days > MAX_AGE_DAYS:
            st["too_old"] += 1
        elif set(c["comic_ids"]) & hc:  # kept apart: only the --loose sensitivity fit reads them
            st["same_issue_as_holdout"] += 1
            cands["same_issue"].append({**c, "same_issue": True})
        else:
            cands[c["pool"]].append(c)
    for lst in cands.values():
        lst.sort(key=lambda r: r["sold_date"], reverse=True)
    cands["same_issue"].sort(key=lambda r: r["pool"] != "slab")  # stable: slabs first, newest first
    (wd / "train_candidates.json").write_text(json.dumps(
        {"ref_date": ref.isoformat(), "holdout_comic_ids": hold_cids, "stats": st, **cands}, indent=1))
    print(st, {p: len(v) for p, v in cands.items()})


# ------------------------------------------------------------------ fetch

def _norm(t: str | None) -> str:
    """Titles compared up to whitespace (eBay pads trailing and doubled spaces)."""
    return " ".join((t or "").split())


def cmd_fetch(wd: Path) -> None:
    t0 = time.time()
    sys.path.insert(0, str(C.EBAY_SRC))
    from ebay_fetch import get_token, load_config

    tc = json.loads((wd / "train_candidates.json").read_text())
    ref = date.fromisoformat(tc["ref_date"])
    full = wd / "train"
    full.mkdir(exist_ok=True)
    res_p = wd / "fetch_results.json"
    results = load(res_p, {})
    todo = (tc["slab"] + tc["raw"] + tc["same_issue"])[:MAX_CALLS]
    # A failure is recorded once and never retried, except a 429 (the app's daily
    # Browse quota), which says nothing about the listing.
    pending = [c for c in todo if c["product_id"] not in results
               or results[c["product_id"]].get("status") == 429
               or results[c["product_id"]].get("error") == "skipped_after_429"]
    print(f"{len(todo)} listings under the cap, {len(pending)} to fetch", flush=True)
    cid, secret, base = load_config()
    box = C.TokenBox(cid, secret, base, get_token(cid, secret, base))
    lock = threading.Lock()
    quota = threading.Event()
    done = 0

    def one(pid):
        if quota.is_set():  # stop spending calls once the daily quota is gone
            return {"product_id": pid, "error": "skipped_after_429"}
        r = C._fetch_one(pid, base, box, full)
        if r.get("status") == 429:
            quota.set()
        return r

    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(one, c["product_id"]): c for c in pending}
        for f in as_completed(futs):
            c = futs[f]
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001 - a crash in a worker is a recorded failure
                r = {"product_id": c["product_id"], "error": f"crash_{type(e).__name__}"}
            r.update(pool=c["pool"], age=age_bucket(c["sold_date"], ref), ledger_title=c["title"])
            if "error" not in r:
                r["title_match"] = _norm(r.get("title")) == _norm(c["title"])
            with lock:
                results[c["product_id"]] = r
                done += 1
                if done % 100 == 0:
                    res_p.write_text(json.dumps(results, indent=1))
                    print(f"{done}/{len(pending)} {time.time() - t0:.0f}s", flush=True)
    res_p.write_text(json.dumps(results, indent=1))
    timing(wd, "fetch", time.time() - t0)
    ok = [r for r in results.values() if "error" not in r]
    print(f"ok {len(ok)} failed {len(results) - len(ok)}; id_match {sum(r['id_match'] for r in ok)}, "
          f"title_match {sum(r['title_match'] for r in ok)}")


# ------------------------------------------------------------------ prep

def _ocr(jpg: bytes) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Apple Vision text boxes: (text, (x, y, w, h)) with y measured from the TOP, in 0..1."""
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(jpg, len(jpg))
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)
    req.setUsesLanguageCorrection_(False)
    h = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    h.performRequests_error_([req], None)
    out = []
    for o in req.results() or []:
        bb = o.boundingBox()
        out.append((str(o.topCandidates_(1)[0].string()),
                    (bb.origin.x, 1 - bb.origin.y - bb.size.height, bb.size.width, bb.size.height)))
    return out


def _leaks(text: str) -> bool:
    return bool(LEAK_WORDS.search(text) or LEAK_ABBR.search(text))


def book_box(im) -> tuple[list[int], str]:
    """Largest-rectangle heuristic (the document-scanner recipe), in three tries:
    1. 'quad': the largest convex 4-gon (approxPolyDP on Canny contours) covering
       20-97% of the frame with a book-like aspect (height/width 0.9-2.2);
    2. 'border': else, pixels far from the median border colour, closed, largest
       component's bounding box under the same size and aspect limits;
    3. 'frame': else, the whole frame (the book usually fills it).
    Returns (x0, y0, x1, y1) in pixels and which try produced it."""
    import cv2
    import numpy as np

    W, H = im.size
    s = 640 / max(W, H)
    rgb = np.asarray(im.resize((max(1, int(W * s)), max(1, int(H * s)))))
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    area = g.shape[0] * g.shape[1]

    def ok(w, h):
        return 0.20 * area <= w * h <= 0.97 * area and 0.9 <= h / max(w, 1) <= 2.2

    def out(x, y, w, h, how):
        return [int(x / s), int(y / s), int(min(W, (x + w) / s)), int(min(H, (y + h) / s))], how

    e = cv2.dilate(cv2.Canny(cv2.GaussianBlur(g, (5, 5), 0), 30, 100), np.ones((3, 3), np.uint8))
    cs, _ = cv2.findContours(e, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cs:
        hull = cv2.convexHull(c)
        q = cv2.approxPolyDP(hull, 0.03 * cv2.arcLength(hull, True), True)
        if len(q) != 4:
            continue
        x, y, w, h = cv2.boundingRect(q)
        if ok(w, h) and cv2.contourArea(q) >= 0.8 * w * h and (best is None or w * h > best[2] * best[3]):
            best = (x, y, w, h)
    if best:
        return out(*best, "quad")
    border = np.concatenate([rgb[:8].reshape(-1, 3), rgb[-8:].reshape(-1, 3),
                             rgb[:, :8].reshape(-1, 3), rgb[:, -8:].reshape(-1, 3)])
    d = np.linalg.norm(rgb.astype(float) - np.median(border, 0), axis=2)
    m = cv2.morphologyEx((d > 40).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(m)
    if n > 1:
        k = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
        x, y, w, h = (int(v) for v in st[k, :4])
        if ok(w, h):
            return out(x, y, w, h, "border")
    return [0, 0, W, H], "frame"


def _prep_listing(pid: str, pool: str, src: Path, front_dir: Path) -> dict:
    from PIL import Image, ImageDraw

    photos = []
    n = json.loads((src / "item.json").read_text())["image_count"]
    for i in range(1, n + 1):
        raw = (src / f"img-{i:02d}.jpg").read_bytes()
        im = Image.open(BytesIO(raw)).convert("RGB")
        if pool == "slab":
            w, h = im.size
            im = im.crop((0, int(h * SLAB_CROP), w, h))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=92)
        boxes = [b for t, b in _ocr(buf.getvalue()) if _leaks(t)]
        p = {"i": i, "sha": hashlib.sha256(raw).hexdigest(), "leak_boxes": len(boxes)}
        if i == 1:
            if boxes:
                d = ImageDraw.Draw(im)
                W, H = im.size
                for x, y, bw, bh in boxes:
                    pad = bh * 0.6
                    d.rectangle([(x - pad) * W, (y - pad) * H, (x + bw + pad) * W, (y + bh + pad) * H], fill=(0, 0, 0))
            out = BytesIO()
            im.save(out, "JPEG", quality=92)
            (front_dir / f"{pid}.jpg").write_bytes(out.getvalue())
            p["box"], p["box_how"] = book_box(im)
            p["dhash"] = dhash(out.getvalue())
            p["keep"] = True
        else:
            p["dhash"] = dhash(buf.getvalue())
            p["keep"] = not boxes
        photos.append(p)
    return {"product_id": pid, "pool": pool, "photos": photos}


def _listings(wd: Path, hd: Path) -> list[tuple[str, str, str, Path]]:
    """(split, product_id, pool, source dir) for every holdout and fetched training listing."""
    man = json.loads((hd / "manifest.json").read_text())
    out = [("test", c["product_id"], c["pool"], hd / "full" / c["product_id"]) for c in man["comps"]]
    res = json.loads((wd / "fetch_results.json").read_text())
    out += [("train", pid, r["pool"], wd / "train" / pid) for pid, r in sorted(res.items())
            if "error" not in r and r.get("id_match") and _norm(r.get("title")) == _norm(r["ledger_title"])]
    return out


def cmd_prep(wd: Path, hd: Path) -> None:
    t0 = time.time()
    fd = wd / "front"
    fd.mkdir(exist_ok=True)
    prep_p = wd / "prep.json"
    prep = load(prep_p, {})
    todo = [x for x in _listings(wd, hd) if x[1] not in prep]
    print(f"prep: {len(todo)} listings to do", flush=True)
    lock = threading.Lock()
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(_prep_listing, pid, pool, src, fd): (split, pid) for split, pid, pool, src in todo}
        for k, f in enumerate(as_completed(futs), 1):
            split, pid = futs[f]
            r = f.result()
            r["split"] = split
            with lock:
                prep[pid] = r
            if k % 200 == 0:
                prep_p.write_text(json.dumps(prep))
                print(f"{k}/{len(todo)} {time.time() - t0:.0f}s", flush=True)
    prep_p.write_text(json.dumps(prep))
    timing(wd, "prep", time.time() - t0)
    for split in ("train", "test"):
        for pool in ("raw", "slab"):
            ls = [v for v in prep.values() if v["split"] == split and v["pool"] == pool]
            ph = [p for v in ls for p in v["photos"]]
            print(f"{split} {pool}: listings {len(ls)}, photos {len(ph)}, dropped for text "
                  f"{sum(not p['keep'] for p in ph)}, front masked {sum(v['photos'][0]['leak_boxes'] > 0 for v in ls)}, "
                  f"crop by quad/border/frame {[sum(v['photos'][0]['box_how'] == h for v in ls) for h in ('quad', 'border', 'frame')]}")


# ------------------------------------------------------------------ embed

def _tiles(im, box):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    c = int(CORNER * w)
    sw = int(SPINE * w)
    out = [im.crop((x0, y0, x0 + c, y0 + c)), im.crop((x1 - c, y0, x1, y0 + c)),
           im.crop((x0, y1 - c, x0 + c, y1)), im.crop((x1 - c, y1 - c, x1, y1))]
    for k in range(3):
        out.append(im.crop((x0, y0 + k * h // 3, x0 + sw, y0 + (k + 1) * h // 3)))
    return out


def cmd_embed(wd: Path, hd: Path, enc: str) -> None:
    import numpy as np
    import torch
    from PIL import Image

    t0 = time.time()
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model, pre, fn = E._encoder(enc, device)
    size = 224
    prep = json.loads((wd / "prep.json").read_text())
    src = {pid: (hd / "full" / pid if v["split"] == "test" else wd / "train" / pid) for pid, v in prep.items()}
    photo_ids, tile_ids = [], []
    for pid in sorted(prep):
        v = prep[pid]
        for p in v["photos"]:
            if p["keep"]:
                photo_ids.append((pid, p["i"]))
        tile_ids += [(pid, t) for t in TILE_NAMES]
    out_p, ids_p = wd / f"emb_{enc}_photos.npy", wd / f"emb_{enc}_photos_ids.json"
    if not (out_p.exists() and load(ids_p, []) == [list(x) for x in photo_ids]):
        def load_photo(x):
            pid, i = x
            if i == 1:
                im = Image.open(wd / "front" / f"{pid}.jpg").convert("RGB")
            else:
                im = Image.open(src[pid] / f"img-{i:02d}.jpg").convert("RGB")
                if prep[pid]["pool"] == "slab":
                    w, h = im.size
                    im = im.crop((0, int(h * SLAB_CROP), w, h))
            im.thumbnail((448, 448))
            return pre(im)
        vecs = _run(photo_ids, load_photo, fn, device, t0, "photos")
        np.save(out_p, vecs)
        ids_p.write_text(json.dumps(photo_ids))
    out_t, ids_t = wd / f"emb_{enc}_tiles.npy", wd / f"emb_{enc}_tiles_ids.json"
    if not (out_t.exists() and load(ids_t, []) == [list(x) for x in tile_ids]):
        cache: dict = {}

        def load_tile(x):
            pid, t = x
            if pid not in cache:
                cache.clear()
                im = Image.open(wd / "front" / f"{pid}.jpg").convert("RGB")
                cache[pid] = [tt.resize((size, size), Image.BICUBIC)
                              for tt in _tiles(im, prep[pid]["photos"][0]["box"])]
            return pre(cache[pid][TILE_NAMES.index(t)])
        vecs = _run(tile_ids, load_tile, fn, device, t0, "tiles", threads=1)
        np.save(out_t, vecs)
        ids_t.write_text(json.dumps(tile_ids))
    timing(wd, f"embed_{enc}", time.time() - t0)
    print(f"embed {enc} on {device}: {time.time() - t0:.0f}s")


def _run(ids, loader, fn, device, t0, label, threads=6):
    import numpy as np
    import torch

    vecs = []
    with ThreadPoolExecutor(threads) as ex, torch.no_grad():
        for i in range(0, len(ids), 64):
            x = torch.stack(list(ex.map(loader, ids[i:i + 64]))).to(device)
            vecs.append(torch.nn.functional.normalize(fn(x).float(), dim=-1).cpu().numpy())
            if (i // 64) % 20 == 0:
                print(f"{label} {i}/{len(ids)} {time.time() - t0:.0f}s", flush=True)
    return np.concatenate(vecs)


# ------------------------------------------------------------------ fit

def _ordinal_fit(X, y, w, wd_, epochs=1500):
    """BUI-1011's CORAL head, also returning the per-rung probabilities."""
    import numpy as np
    import torch

    torch.manual_seed(0)
    K = len(LADDER)
    Xt, yt = torch.tensor(X, dtype=torch.float32), torch.tensor(y)
    wt = torch.tensor(w, dtype=torch.float32)
    lin = torch.nn.Linear(X.shape[1], 1)
    t0 = torch.nn.Parameter(torch.tensor([-2.0]))
    inc = torch.nn.Parameter(torch.full((K - 2,), -1.0))
    opt = torch.optim.Adam([{"params": lin.parameters(), "weight_decay": wd_},
                            {"params": [t0, inc], "weight_decay": 0.0}], lr=0.05)
    targets = (yt[:, None] > torch.arange(K - 1)[None, :]).float()
    for _ in range(epochs):
        th = torch.cat([t0, t0 + torch.cumsum(torch.nn.functional.softplus(inc), 0)])
        logits = lin(Xt) - th[None, :]
        loss = (torch.nn.functional.binary_cross_entropy_with_logits(
            logits, targets, reduction="none").sum(1) * wt).sum() / wt.sum()
        opt.zero_grad()
        loss.backward()
        opt.step()
    with torch.no_grad():
        th = torch.cat([t0, t0 + torch.cumsum(torch.nn.functional.softplus(inc), 0)])

    def predict(Xn):
        """(predicted rung, confidence = probability mass within +/-1.0 of it, rung probabilities)."""
        with torch.no_grad():
            gt = torch.sigmoid(lin(torch.tensor(Xn, dtype=torch.float32)) - th[None, :]).numpy()
        pred = (gt > 0.5).sum(1)
        ext = np.hstack([np.ones((len(gt), 1)), gt, np.zeros((len(gt), 1))])
        prob = np.clip(ext[:, :-1] - ext[:, 1:], 0, 1)
        lad = np.array(LADDER)
        near = np.abs(lad[None, :] - lad[pred][:, None]) <= 1.0 + 1e-9
        return pred, (prob * near).sum(1), prob
    return predict


def _design(wd: Path, hd: Path, enc: str, loose: bool = False):
    """Leakage filters and the per-arm feature blocks for train and holdout."""
    import numpy as np

    prep = json.loads((wd / "prep.json").read_text())
    man = {c["product_id"]: c for c in json.loads((hd / "manifest.json").read_text())["comps"]}
    tc = json.loads((wd / "train_candidates.json").read_text())
    cand = {c["product_id"]: c for c in tc["raw"] + tc["slab"] + tc["same_issue"]}
    P = np.load(wd / f"emb_{enc}_photos.npy")
    pids = json.loads((wd / f"emb_{enc}_photos_ids.json").read_text())
    T = np.load(wd / f"emb_{enc}_tiles.npy")
    tids = json.loads((wd / f"emb_{enc}_tiles_ids.json").read_text())
    row = {(p, i): k for k, (p, i) in enumerate(pids)}
    trow = {(p, t): k for k, (p, t) in enumerate(tids)}

    # boilerplate photos (seller banners, stock backs): a dHash within 2 bits of
    # photos in >= 3 distinct listings; label-free, ignored on both sides.
    dh = [(pid, p["i"], p["dhash"]) for pid, v in prep.items() for p in v["photos"] if p["keep"] and p["i"] > 1]
    buckets: dict[int, set] = {}
    for pid, i, h in dh:
        buckets.setdefault(h, set()).add(pid)
    boiler = set()
    hs = list(buckets)
    for h in hs:
        near = set().union(*(buckets[g] for g in hs if bin(h ^ g).count("1") <= 2))
        if len(near) >= 3:
            boiler.add(h)

    def kept(pid):
        return [p for p in prep[pid]["photos"] if p["keep"] and (p["i"] == 1 or p["dhash"] not in boiler)]

    test = sorted(pid for pid, v in prep.items() if v["split"] == "test")
    train = sorted(pid for pid, v in prep.items() if v["split"] == "train")
    assert len(test) == 150
    hold_sha = {p["sha"] for pid in test for p in prep[pid]["photos"]}
    hold_dh = [p["dhash"] for pid in test for p in kept(pid)]
    hold_front = P[[row[(pid, 1)] for pid in test]]
    drop = {"same_issue": 0, "pid": 0, "sha": 0, "dhash": 0, "cos>=0.95": 0, "dup_in_train": 0}
    keep, seen = [], set()
    for pid in train:
        ph = kept(pid)
        fr = P[row[(pid, 1)]]
        if cand[pid].get("same_issue") and not loose:
            drop["same_issue"] += 1
        elif pid in man:
            drop["pid"] += 1
        elif any(p["sha"] in hold_sha for p in prep[pid]["photos"]):
            drop["sha"] += 1
        elif any(min(bin(p["dhash"] ^ h).count("1") for h in hold_dh) <= 6 for p in ph):
            drop["dhash"] += 1
        elif float((hold_front @ fr).max()) >= 0.95:
            drop["cos>=0.95"] += 1
        elif prep[pid]["photos"][0]["sha"] in seen:
            drop["dup_in_train"] += 1
        else:
            seen.add(prep[pid]["photos"][0]["sha"])
            keep.append(pid)

    def blocks(pid):
        ph = kept(pid)
        V = P[[row[(pid, p["i"])] for p in ph]]
        tv = T[[trow[(pid, t)] for t in TILE_NAMES]]
        return {"front": P[row[(pid, 1)]],
                "whole": np.concatenate([V.mean(0), V.max(0)]),
                "tiles": np.concatenate([tv[:4].reshape(-1), tv[4:].mean(0)]),
                "n_photos": len(ph)}
    meta = {pid: {"pool": cand[pid]["pool"], "grade": cand[pid]["grade"], "comic_ids": cand[pid]["comic_ids"]}
            for pid in keep}
    meta.update({pid: {"pool": man[pid]["pool"], "grade": man[pid]["grade"], "rid": man[pid]["rid"],
                       "band": man[pid]["band"]} for pid in test})
    B = {pid: blocks(pid) for pid in keep + test}
    return keep, test, meta, B, drop, len(boiler)


def _X(B, ids, meta, arm):
    import numpy as np

    parts = {"front": ["front"], "whole": ["whole"], "tiles": ["tiles"], "both": ["whole", "tiles"]}[arm]
    return np.array([np.concatenate([B[p][k] for k in parts] + [[float(meta[p]["pool"] == "slab")]]) for p in ids])


def cmd_fit(wd: Path, hd: Path, enc: str, arms: list[str], loose: bool = False) -> None:
    import numpy as np
    from sklearn.model_selection import StratifiedGroupKFold

    t0 = time.time()
    keep, test, meta, B, drop, n_boiler = _design(wd, hd, enc, loose)
    print("leakage drops", drop, "boilerplate hashes", n_boiler, "train", len(keep), flush=True)
    pools = np.array([meta[p]["pool"] for p in keep])
    grades = np.array([meta[p]["grade"] for p in keep])
    y = np.array([rung(g) for g in grades])
    groups = [meta[p]["comic_ids"][0] if meta[p]["comic_ids"] else f"pid{p}" for p in keep]
    strat = [f"{p}{band_of(g, p)}" for p, g in zip(pools, grades)]
    folds = list(StratifiedGroupKFold(5, shuffle=True, random_state=1013).split(np.zeros(len(y)), strat, groups))
    for arm in arms:
        X = _X(B, keep, meta, arm)
        cv = []
        oof = {}
        # 0.01 and 0.03 were added after the first run's 'both' pick sat on the
        # 0.1 grid edge (CV numbers only, before any holdout metric was read).
        for wdk in (0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0):
            for bal in (False, True):
                pred, conf = np.zeros(len(y), dtype=int), np.zeros(len(y))
                for tri, vai in folds:
                    mu, sd = X[tri].mean(0), X[tri].std(0) + 1e-6
                    f = _ordinal_fit((X[tri] - mu) / sd, y[tri], E._weights(y[tri], pools[tri], bal), wdk)
                    pred[vai], conf[vai], _ = f((X[vai] - mu) / sd)
                err = np.array([LADDER[p] for p in pred]) - grades
                cv.append({"hp": wdk, "balanced": bal,
                           "bb_within1": round(E._band_balanced_hit(pred, grades, pools), 3),
                           "within1": round(float((np.abs(err) <= 1.0).mean()), 3),
                           "mae": round(float(np.abs(err).mean()), 3)})
                oof[(wdk, bal)] = (pred.tolist(), conf.tolist())
                print(arm, cv[-1], flush=True)
        ch = max(cv, key=lambda c: c["bb_within1"])
        mu, sd = X.mean(0), X.std(0) + 1e-6
        f = _ordinal_fit((X - mu) / sd, y, E._weights(y, pools, ch["balanced"]), ch["hp"])
        pr, cf, _ = f((_X(B, test, meta, arm) - mu) / sd)
        op, oc = oof[(ch["hp"], ch["balanced"])]
        out = {"encoder": enc, "arm": arm, "dim": int(X.shape[1]), "leak_drops": drop, "boilerplate": n_boiler,
               "n_train": {"raw": int((pools == "raw").sum()), "slab": int((pools == "slab").sum())},
               "train_by_band": {p: [int(sum(1 for q, g in zip(pools, grades) if q == p and band_of(g, p) == b))
                                     for b in range(6)] for p in ("raw", "slab")},
               "cv": cv, "chosen": ch,
               "oof": [{"pid": p, "pool": q, "grade": float(g), "pred": LADDER[int(a)], "conf": float(c)}
                       for p, q, g, a, c in zip(keep, pools, grades, op, oc)],
               "preds": {meta[p]["rid"]: {"pid": p, "pred": LADDER[int(a)], "conf": float(c)}
                         for p, a, c in zip(test, pr, cf)}}
        (wd / f"preds_{enc}_{arm}{'_loose' if loose else ''}.json").write_text(json.dumps(out, indent=1))
        print(f"{arm}: chosen {ch} {time.time() - t0:.0f}s", flush=True)
    timing(wd, f"fit_{enc}", time.time() - t0)


# ------------------------------------------------------------------ metrics

def _hit(r):
    return abs(r["pred"] - r["grade"]) <= 1.0


def _coverage(rows, fracs=(0.25, 0.40, 0.5, 0.75, 1.0)):
    rows = sorted(rows, key=lambda r: -r["conf"])
    out = []
    for f in fracs:
        sub = rows[: max(1, round(f * len(rows)))]
        out.append((f, len(sub), sum(map(_hit, sub)) / len(sub)))
    return out


def cmd_metrics(wd: Path, hd: Path, enc: str, arm: str, loose: bool = False) -> None:
    res = json.loads((wd / f"preds_{enc}_{arm}{'_loose' if loose else ''}.json").read_text())
    key = json.loads((hd / "bui-1006" / "key.json").read_text())
    haiku = {r["product_id"]: r["pred"] for r in json.loads((hd / "bui-1006" / "joined.json").read_text())}
    b11 = {t: json.loads((hd / "bui-1011" / f"preds_{t}.json").read_text())["preds"]["ordinal"]
           for t in ("l14", "l14_strict")}
    rows = [{**key[rid], "pred": v["pred"], "conf": v["conf"], "haiku": haiku[key[rid]["product_id"]],
             "b11": b11["l14"][rid], "b11s": b11["l14_strict"][rid]} for rid, v in res["preds"].items()]
    assert len(rows) == 150 and all(key[r]["product_id"] == v["pid"] for r, v in res["preds"].items())
    print(f"## {enc} {arm}{' LOOSE' if loose else ''}: dim {res['dim']}, n_train {res['n_train']}, by band {res['train_by_band']}")
    print(f"CV chosen {res['chosen']}; leak drops {res['leak_drops']}; boilerplate {res['boilerplate']}\n")
    hdr = "| Cut | n | MAE | within ±1.0 | within ±0.5 | bias | Pearson r | QWK |\n|---|---|---|---|---|---|---|---|"
    print(hdr)
    for name, sel in (("Raw", "raw"), ("Slab", "slab"), ("Combined", None)):
        print(f"| {name} " + E._row([(r["pred"], r["grade"]) for r in rows if sel in (None, r["pool"])]))
    for pool, bands in (("raw", RAW_BANDS), ("slab", SLAB_BANDS)):
        print(f"\n{pool} by band\n" + hdr)
        for b, (lo, hi) in enumerate(bands):
            print(f"| {lo}-{hi} " + E._row([(r["pred"], r["grade"]) for r in rows if r["pool"] == pool and r["band"] == b]))
    m = [_hit(r) for r in rows]
    for name, k in (("Haiku", "haiku"), ("BUI-1011 L-14", "b11"), ("BUI-1011 L-14 strict", "b11s")):
        o = [abs(r[k] - r["grade"]) <= 1.0 for r in rows]
        print(f"paired vs {name} ({sum(o)}/150): fixes {sum(a and not b for a, b in zip(m, o))}, "
              f"breaks {sum(b and not a for a, b in zip(m, o))}, both miss {sum(not a and not b for a, b in zip(m, o))}")
    p = [r["pred"] for r in rows]
    print(f"pred spread stdev {statistics.pstdev(p):.2f} (true {statistics.pstdev([r['grade'] for r in rows]):.2f})\n")
    print("selective coverage (holdout, ranked by confidence within each cut):")
    for name, sel in (("raw", "raw"), ("slab", "slab"), ("all", None)):
        cov = _coverage([r for r in rows if sel in (None, r["pool"])])
        print(f"  {name}: " + ", ".join(f"{f:.0%} (n={n}) {a:.0%}" for f, n, a in cov))
    oof = res["oof"]
    thr = sorted((o["conf"] for o in oof), reverse=True)[round(0.40 * len(oof)) - 1]
    sel = [r for r in rows if r["conf"] >= thr]
    print(f"  CV-set threshold for 40% coverage = {thr:.3f}: holdout coverage {len(sel)}/150, "
          f"within ±1.0 {sum(map(_hit, sel)) / max(1, len(sel)):.0%}")
    print("calibration (accuracy by confidence quintile, low -> high):")
    for name, rs in (("CV out-of-fold", [{**o} for o in oof]), ("holdout", rows)):
        for pool in ("raw", "slab", None):
            sub = sorted([r for r in rs if pool in (None, r["pool"])], key=lambda r: r["conf"])
            q = [sub[i * len(sub) // 5:(i + 1) * len(sub) // 5] for i in range(5)]
            print(f"  {name} {pool or 'all'} (n={len(sub)}): " + " ".join(f"{sum(map(_hit, x)) / len(x):.0%}" for x in q))
    # does the curve just re-sort pools/bands? share of each pool in the top 40%
    top = sorted(rows, key=lambda r: -r["conf"])[:60]
    print(f"  top-40% holdout mix: raw {sum(r['pool'] == 'raw' for r in top)}, slab {sum(r['pool'] == 'slab' for r in top)}; "
          f"by true band {[sum(r['band'] == b for r in top) for b in range(6)]}")
    # within the high bands alone (8.0 and up), does confidence still rank hits above misses?
    hi = sorted([r for r in rows if r["band"] >= 3], key=lambda r: -r["conf"])
    h = len(hi) // 2
    print(f"  bands 8.0+ only (n={len(hi)}): top half by confidence {sum(map(_hit, hi[:h])) / h:.0%}, "
          f"bottom half {sum(map(_hit, hi[h:])) / (len(hi) - h):.0%}")
    oofc = sorted(oof, key=lambda r: -r["conf"])
    print("  CV out-of-fold coverage (natural recent-ledger mix): " + ", ".join(
        f"{f:.0%} {sum(map(_hit, oofc[:round(f * len(oofc))])) / round(f * len(oofc)):.0%}"
        for f in (0.25, 0.40, 0.5, 0.75, 1.0)))
    # Sensitivity, not the bar: the holdout is band-stratified; reweight each book by
    # its (pool, band) cell's share of the training set (the recent ledger's mix).
    cells = [(o["pool"], band_of(o["grade"], o["pool"])) for o in oof]
    share = {c: cells.count(c) / len(cells) for c in set(cells)}
    hcnt = {c: sum((r["pool"], r["band"]) == c for r in rows) for c in share}
    wts = sorted(((share[(r["pool"], r["band"])] / hcnt[(r["pool"], r["band"])], r) for r in rows),
                 key=lambda t: -t[1]["conf"])
    tot, out = sum(w for w, _ in wts), []
    for f in (0.25, 0.40, 0.5, 0.75, 1.0):
        acc_w = acc_h = 0.0
        for w, r in wts:
            if acc_w >= f * tot - 1e-12:
                break
            acc_w += w
            acc_h += w * _hit(r)
        out.append(f"{f:.0%} {acc_h / acc_w:.0%}")
    print("  holdout reweighted to the ledger mix (sensitivity): " + ", ".join(out))


# ------------------------------------------------------------------ sheet

def cmd_sheet(wd: Path, hd: Path, split: str, n: int, seed: int, what: str) -> None:
    """Contact sheets: 'crop' draws the book box and tile outlines on masked fronts;
    'slab' shows kept non-front slab photos (after the top crop) for a label check."""
    import random

    from PIL import Image, ImageDraw

    prep = json.loads((wd / "prep.json").read_text())
    rnd = random.Random(seed)
    ids = sorted(p for p, v in prep.items() if v["split"] == split)
    thumbs = []
    if what == "crop":
        for pid in rnd.sample(ids, n):
            im = Image.open(wd / "front" / f"{pid}.jpg").convert("RGB")
            d = ImageDraw.Draw(im)
            x0, y0, x1, y1 = prep[pid]["photos"][0]["box"]
            d.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=14)
            c, sw = int(CORNER * (x1 - x0)), int(SPINE * (x1 - x0))
            for bx in ((x0, y0), (x1 - c, y0), (x0, y1 - c), (x1 - c, y1 - c)):
                d.rectangle([bx[0], bx[1], bx[0] + c, bx[1] + c], outline=(0, 255, 0), width=5)
            d.rectangle([x0, y0, x0 + sw, y1], outline=(0, 128, 255), width=5)
            im.thumbnail((300, 300))
            thumbs.append((f"{pid} {prep[pid]['photos'][0]['box_how']}", im))
    else:
        cand = [(pid, p["i"]) for pid in ids if prep[pid]["pool"] == "slab" for p in prep[pid]["photos"]
                if p["keep"] and p["i"] > 1]
        for pid, i in rnd.sample(cand, min(n, len(cand))):
            base = (hd / "full" if split == "test"
                    else wd / "train") / pid
            im = Image.open(base / f"img-{i:02d}.jpg").convert("RGB")
            w, h = im.size
            im = im.crop((0, int(h * SLAB_CROP), w, h))
            im.thumbnail((300, 300))
            thumbs.append((f"{pid}/{i}", im))
    cols = 5
    rows_ = (len(thumbs) + cols - 1) // cols
    S = Image.new("RGB", (cols * 300, rows_ * 320), "white")
    d = ImageDraw.Draw(S)
    for k, (lab, im) in enumerate(thumbs):
        S.paste(im, ((k % cols) * 300, (k // cols) * 320))
        d.text(((k % cols) * 300 + 4, (k // cols) * 320 + 302), lab, fill=(0, 0, 0))
    out = wd / f"sheet_{what}_{split}_{seed}.jpg"
    S.save(out, quality=85)
    print(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["sample", "fetch", "prep", "embed", "fit", "metrics", "sheet"])
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--holdout", required=True, type=lambda p: Path(p).expanduser())
    ap.add_argument("--encoder", choices=sorted(E.ENCODERS), default="l14")
    ap.add_argument("--arm", action="append", choices=ARMS, help="fit/metrics: arm(s) (default all for fit)")
    ap.add_argument("--split", default="train", choices=["train", "test"])
    ap.add_argument("--what", default="crop", choices=["crop", "slab"])
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--loose", action="store_true",
                    help="fit/metrics: also train on comps sharing a comic_id with a holdout comp (BUI-1011 default)")
    a = ap.parse_args()
    a.workdir.mkdir(parents=True, exist_ok=True)
    if a.step == "sample":
        cmd_sample(a.workdir, a.holdout)
    elif a.step == "fetch":
        cmd_fetch(a.workdir)
    elif a.step == "prep":
        cmd_prep(a.workdir, a.holdout)
    elif a.step == "embed":
        cmd_embed(a.workdir, a.holdout, a.encoder)
    elif a.step == "fit":
        cmd_fit(a.workdir, a.holdout, a.encoder, a.arm or list(ARMS), a.loose)
    elif a.step == "metrics":
        for arm in a.arm or ARMS:
            cmd_metrics(a.workdir, a.holdout, a.encoder, arm, a.loose)
    else:
        cmd_sheet(a.workdir, a.holdout, a.split, a.n, a.seed, a.what)


if __name__ == "__main__":
    main()
