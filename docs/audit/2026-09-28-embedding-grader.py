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
# ]
# ///
"""Measure a frozen-encoder + ordinal-head grader on labeled sold comps (BUI-1011).

DIAGNOSTIC ONLY. The comics DB is opened read-only; nothing is written to it.
The test set is BUI-1006's 150 holdout comps and their exact prepped images,
read from --holdout (never modified). Every other file lives in --workdir.

Steps (subcommands), each resumable:

  sample   Training candidates: every labeled comp with no excluded_code, same
           title filters and slab rule as BUI-1006 cmd_sample, deduped on
           product_id across the whole ledger (a product_id carrying two
           different grades is dropped), minus every holdout product_id and
           BUI-1006's rejected one. Newest sold_date first per pool.
  fetch    Browse API (ebay_fetch.fetch_item_with_status), first image only,
           prepped exactly as BUI-1006 (_prep_image: slab top-27% crop, 768 px,
           JPEG q80). 4 worker threads, one shared token refreshed once per 401.
           Hard cap MAX_CALLS Browse calls in total (slabs first, up to
           SLAB_CALLS). Every failure is recorded with its sold-date age bucket.
  embed    Frozen encoder on MPS, L2-normalised features cached as .npy for
           the training images and the 150 holdout images.
  fit      Leakage filter (exact prepped-image hash and 64-bit dHash against
           the holdout; exact-hash dedupe inside training), then 5-fold
           StratifiedGroupKFold CV on TRAINING ONLY (groups = comic_id) to pick
           head + hyperparameters by band-balanced within-1.0. The chosen
           configuration is refit on all training rows and scores the holdout
           once. Writes preds_<encoder>.json.
  curve    Learning curve on training-set CV only (25-100% of each fold).
  metrics  BUI-1006's tables for the chosen predictions, the median-guess
           baseline, and a paired comparison against Haiku (joined.json).

    uv run docs/audit/2026-09-28-embedding-grader.py <step> --workdir W --holdout H
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

DB = os.path.expanduser("~/.comics-server/db.sqlite")
EBAY_SRC = Path(__file__).resolve().parents[2] / "apps" / "ebay" / "src"

# Copied from docs/audit/2026-09-28-photo-grading-accuracy.py so train and
# test share the candidate filters and the preprocessing byte for byte.
RAW_BANDS = [(0.5, 3.5), (4.0, 5.5), (6.0, 7.5), (8.0, 8.5), (9.0, 9.2), (9.4, 10.0)]
SLAB_BANDS = [(2.0, 3.5), (4.0, 5.5), (6.0, 7.5), (8.0, 8.5), (9.0, 9.4), (9.6, 10.0)]
LONG_EDGE = 768
JPEG_Q = 80
SLAB_CROP = 0.27
LOT_RE = re.compile(
    r"\blots?\b|\bsets?\b|\bruns?\b|\bbundle|\bcollection\b|complete|"
    r"#\s*\d+\s*-\s*#?\s*\d+|\b\d+\s*(comics|books|issues)\b|\bx\s?\d\b|"
    r"#\d+\s*(&|and|,)\s*#?\d+|\bpick\b|\bchoose\b|\byou pick\b",
    re.I,
)
SLAB_RE = re.compile(r"\b(cgc|cbcs|pgx|slab|graded|egs)\b", re.I)

LADDER = [x / 2 for x in range(1, 19)] + [9.2, 9.4, 9.6, 9.8, 10.0]  # 0.5..9.0, then 9.2..10.0
MAX_CALLS = 3500
SLAB_CALLS = 1000
WORKERS = 4
ENCODERS = {
    "b32": ("open_clip", "ViT-B-32", "laion2b_s34b_b79k"),
    "l14": ("open_clip", "ViT-L-14", "laion2b_s32b_b82k"),
    "dino": ("timm", "vit_small_patch14_dinov2.lvd142m", None),
}


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


def age_bucket(sold: str, ref: date) -> str:
    if not sold:
        return "unknown"
    d = (ref - date.fromisoformat(sold)).days
    return "<=60" if d <= 60 else "61-90" if d <= 90 else "91-120" if d <= 120 else ">120"


def band_of(g: float, pool: str) -> int | None:
    for i, (lo, hi) in enumerate(RAW_BANDS if pool == "raw" else SLAB_BANDS):
        if lo - 1e-9 <= g <= hi + 1e-9:
            return i
    return None


def rung(g: float) -> int:
    """Index of the nearest ladder rung (1.8 -> 2.0, 8.8 -> 9.0)."""
    return min(range(len(LADDER)), key=lambda i: (abs(LADDER[i] - g), -i))


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


def dhash(jpg: bytes) -> int:
    from PIL import Image

    im = Image.open(BytesIO(jpg)).convert("L").resize((9, 8))
    px = list(im.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


def load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def timing(wd: Path, step: str, secs: float) -> None:
    t = load(wd / "timings.json", {})
    t[step] = round(t.get(step, 0) + secs, 1)
    (wd / "timings.json").write_text(json.dumps(t, indent=1))


# ------------------------------------------------------------------ sample

def cmd_sample(wd: Path, hd: Path) -> None:
    import sqlite3

    holdout = load(hd / "key.json", {})
    banned = {v["product_id"] for v in holdout.values()} | set(load(hd / "rejected.json", []))
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT product_id, comic_id, title, grade, pool, sold_date, certifier, label "
        "FROM comps WHERE grade IS NOT NULL AND excluded_code IS NULL"
    ).fetchall()
    # comic_ids of the holdout (for the stricter same-issue sensitivity cut)
    hold_cids = sorted({c for (pid, c) in conn.execute(
        "SELECT product_id, comic_id FROM comps WHERE comic_id IS NOT NULL") if pid in banned})
    conn.close()
    by_pid: dict[str, dict] = {}
    bad: set[str] = set()
    stats = {"rows": len(rows), "holdout_or_rejected": 0, "filtered": 0, "grade_conflict": 0}
    for pid, cid, title, grade, pool, sold, cert, label in rows:
        title = title or ""
        if pid in banned:
            stats["holdout_or_rejected"] += 1
            continue
        if pool == "raw":
            if LOT_RE.search(title) or SLAB_RE.search(title):
                stats["filtered"] += 1
                continue
        elif label != "universal" or cert not in ("cgc", "cbcs"):
            stats["filtered"] += 1
            continue
        c = by_pid.get(pid)
        if c is None:
            by_pid[pid] = {"product_id": pid, "title": title, "grade": grade, "pool": pool,
                           "sold_date": parse_date(sold), "comic_ids": [cid] if cid is not None else []}
        elif c["grade"] != grade or c["pool"] != pool:
            bad.add(pid)
        else:  # same listing under another comic_id row: one training example
            if cid is not None and cid not in c["comic_ids"]:
                c["comic_ids"].append(cid)
            c["sold_date"] = max(c["sold_date"], parse_date(sold))
    for pid in bad:
        by_pid.pop(pid, None)
    stats["grade_conflict"] = len(bad)
    cands = {"raw": [], "slab": []}
    for c in by_pid.values():
        cands[c["pool"]].append(c)
    for lst in cands.values():
        lst.sort(key=lambda r: r["sold_date"], reverse=True)
    ref = date.today().isoformat()
    (wd / "train_candidates.json").write_text(json.dumps(
        {"ref_date": ref, "holdout_comic_ids": hold_cids, "stats": stats, **cands}, indent=1))
    print(stats, {p: len(v) for p, v in cands.items()})
    for pool, lst in cands.items():
        by = {}
        for c in lst:
            by[age_bucket(c["sold_date"], date.fromisoformat(ref))] = by.get(age_bucket(c["sold_date"], date.fromisoformat(ref)), 0) + 1
        print(pool, by)


# ------------------------------------------------------------------ fetch

def cmd_fetch(wd: Path) -> None:
    import requests

    t0 = time.time()
    sys.path.insert(0, str(EBAY_SRC))
    from ebay_fetch import fetch_item_with_status, get_token, load_config

    tc = json.loads((wd / "train_candidates.json").read_text())
    ref = date.fromisoformat(tc["ref_date"])
    key_p, fail_p = wd / "train_key.json", wd / "train_failures.json"
    key, failures = load(key_p, {}), load(fail_p, [])
    tried = set(key) | {f["product_id"] for f in failures}
    img_dir = wd / "train_images"
    img_dir.mkdir(exist_ok=True)
    budget = MAX_CALLS - len(tried)
    slab_budget = max(0, SLAB_CALLS - sum(1 for v in list(key.values()) + failures if v["pool"] == "slab"))
    todo = [c for c in tc["slab"] if c["product_id"] not in tried][:slab_budget]
    todo += [c for c in tc["raw"] if c["product_id"] not in tried][: max(0, budget - len(todo))]
    todo = todo[:max(0, budget)]
    print(f"already tried {len(tried)}; this run {len(todo)} (cap {MAX_CALLS})", flush=True)

    cid, secret, base = load_config()
    tok = {"v": get_token(cid, secret, base)}
    lock = threading.Lock()

    def one(c: dict) -> dict:
        used = tok["v"]
        calls = 1
        data, status = fetch_item_with_status(c["product_id"], used, base, retries=2)
        if data is None and status == 401:
            with lock:
                if tok["v"] == used:
                    tok["v"] = get_token(cid, secret, base, force_refresh=True)
            calls += 1
            data, status = fetch_item_with_status(c["product_id"], tok["v"], base, retries=2)
        out = {**c, "age": age_bucket(c["sold_date"], ref), "calls": calls}
        url = ((data or {}).get("image") or {}).get("imageUrl")
        if data is None:
            return {**out, "reason": f"browse_{status}"}
        if not url:
            return {**out, "reason": "no_image"}
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            jpg = _prep_image(r.content, c["pool"])
        except Exception as e:  # noqa: BLE001 - recorded as a failure, never dropped
            return {**out, "reason": f"image_{type(e).__name__}"}
        (img_dir / f"{c['product_id']}.jpg").write_bytes(jpg)
        return {**out, "url": url, "sha_raw": hashlib.sha256(r.content).hexdigest(),
                "sha_prep": hashlib.sha256(jpg).hexdigest(), "dhash": dhash(jpg)}

    done = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(one, c) for c in todo]
        for f in as_completed(futs):
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001 - a crash in a worker must still count
                failures.append({"product_id": "?", "pool": "?", "age": "unknown", "reason": f"crash_{type(e).__name__}"})
                continue
            if "reason" in res:
                failures.append(res)
            else:
                key[res["product_id"]] = res
            done += 1
            if done % 100 == 0:
                key_p.write_text(json.dumps(key, indent=1))
                fail_p.write_text(json.dumps(failures, indent=1))
                print(f"{done}/{len(todo)} ok={len(key)} fail={len(failures)} {time.time() - t0:.0f}s", flush=True)
    key_p.write_text(json.dumps(key, indent=1))
    fail_p.write_text(json.dumps(failures, indent=1))
    timing(wd, "fetch", time.time() - t0)
    n_ok, n_fail = len(key), len(failures)
    assert n_ok + n_fail == len(tried) + len(todo), "attempt accounting drifted"
    print(f"fetched {n_ok}, failed {n_fail} ({n_fail / max(1, n_ok + n_fail):.1%})")


# ------------------------------------------------------------------ embed

def _encoder(name: str, device):
    kind, arch, tag = ENCODERS[name]
    if kind == "open_clip":
        import open_clip

        model, _, pre = open_clip.create_model_and_transforms(arch, pretrained=tag)
        return model.to(device).eval(), pre, model.encode_image
    import timm

    model = timm.create_model(arch, pretrained=True, num_classes=0)
    cfg = timm.data.resolve_data_config({}, model=model)
    pre = timm.data.create_transform(**cfg)
    return model.to(device).eval(), pre, model


def cmd_embed(wd: Path, hd: Path, enc: str) -> None:
    import numpy as np
    import torch
    from PIL import Image

    t0 = time.time()
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    _, pre, fn = _encoder(enc, device)
    key = json.loads((wd / "train_key.json").read_text())
    hold = json.loads((hd / "key.json").read_text())
    sets = {"train": [(pid, wd / "train_images" / f"{pid}.jpg") for pid in sorted(key)],
            "test": [(rid, hd / "images" / f"{rid}.jpg") for rid in sorted(hold)]}
    for split, items in sets.items():
        out = wd / f"emb_{enc}_{split}.npy"
        ids_p = wd / f"emb_{enc}_{split}_ids.json"
        if out.exists() and load(ids_p, []) == [i for i, _ in items]:
            continue
        vecs = []
        with torch.no_grad():
            for i in range(0, len(items), 32):
                x = torch.stack([pre(Image.open(p).convert("RGB")) for _, p in items[i:i + 32]]).to(device)
                v = fn(x).float()
                vecs.append(torch.nn.functional.normalize(v, dim=-1).cpu().numpy())
        np.save(out, np.concatenate(vecs))
        ids_p.write_text(json.dumps([i for i, _ in items]))
        print(split, len(items), flush=True)
    timing(wd, f"embed_{enc}", time.time() - t0)
    print(f"embed {enc} on {device}: {time.time() - t0:.0f}s")


# ------------------------------------------------------------------ fit

def _features(emb, slab):
    import numpy as np

    return np.hstack([emb, slab[:, None].astype(np.float32)])


def _ordinal_fit(X, y, w, wd_, epochs=1500):
    """Cumulative-link (CORAL) head: one linear score, K-1 monotone thresholds."""
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
        loss = (torch.nn.functional.binary_cross_entropy_with_logits(logits, targets, reduction="none").sum(1) * wt).sum() / wt.sum()
        opt.zero_grad()
        loss.backward()
        opt.step()
    with torch.no_grad():
        th = torch.cat([t0, t0 + torch.cumsum(torch.nn.functional.softplus(inc), 0)])

    def predict(Xn):
        with torch.no_grad():
            s = lin(torch.tensor(Xn, dtype=torch.float32))
            return (s > th[None, :]).sum(1).numpy()
    return predict


def _ridge_fit(X, y, w, alpha):
    import numpy as np
    from sklearn.linear_model import Ridge

    m = Ridge(alpha=alpha).fit(X, np.array([LADDER[i] for i in y]), sample_weight=w)
    lad = np.array(LADDER)
    return lambda Xn: np.abs(np.clip(m.predict(Xn), 0.5, 10.0)[:, None] - lad[None, :]).argmin(1)


def _weights(y_rung, pools, balanced):
    import numpy as np

    if not balanced:
        return np.ones(len(y_rung))
    keys = [(p, band_of(LADDER[r], p) if band_of(LADDER[r], p) is not None else -1) for r, p in zip(y_rung, pools)]
    cnt = {k: keys.count(k) for k in set(keys)}
    w = np.array([1.0 / cnt[k] for k in keys])
    return w / w.mean()


def _band_balanced_hit(pred_rung, true_grade, pools):
    """Mean over (pool, band) cells of within-1.0: mimics the holdout's stratification."""
    cells: dict = {}
    for p, t, pool in zip(pred_rung, true_grade, pools):
        b = band_of(t, pool)
        if b is None:
            continue
        cells.setdefault((pool, b), []).append(float(abs(LADDER[p] - t) <= 1.0))
    return statistics.mean(statistics.mean(v) for v in cells.values())


def cmd_fit(wd: Path, hd: Path, enc: str, strict_issue: bool) -> None:
    import numpy as np
    from sklearn.model_selection import StratifiedGroupKFold

    t0 = time.time()
    key = json.loads((wd / "train_key.json").read_text())
    tc = json.loads((wd / "train_candidates.json").read_text())
    hold = json.loads((hd / "key.json").read_text())
    tr_ids = json.loads((wd / f"emb_{enc}_train_ids.json").read_text())
    te_ids = json.loads((wd / f"emb_{enc}_test_ids.json").read_text())
    Etr, Ete = np.load(wd / f"emb_{enc}_train.npy"), np.load(wd / f"emb_{enc}_test.npy")

    # ---- leakage controls
    hold_pids = {v["product_id"] for v in hold.values()}
    hold_sha, hold_dh = set(), []
    for rid in te_ids:
        b = (hd / "images" / f"{rid}.jpg").read_bytes()
        hold_sha.add(hashlib.sha256(b).hexdigest())
        hold_dh.append(dhash(b))
    hold_cids = set(tc["holdout_comic_ids"])
    drop = {"pid": 0, "sha": 0, "dhash": 0, "cos>=0.95": 0, "dup_in_train": 0, "same_issue": 0}
    cos_all = (Etr @ Ete.T).max(1)  # nearest holdout image per training image (label-free)
    keep, seen_sha = [], set()
    for i, pid in enumerate(tr_ids):
        k = key[pid]
        if pid in hold_pids:
            drop["pid"] += 1
        elif k["sha_prep"] in hold_sha:
            drop["sha"] += 1
        elif min(bin(k["dhash"] ^ h).count("1") for h in hold_dh) <= 6:
            drop["dhash"] += 1
        elif cos_all[i] >= 0.95:
            drop["cos>=0.95"] += 1
        elif k["sha_raw"] in seen_sha:
            drop["dup_in_train"] += 1
        elif strict_issue and set(k["comic_ids"]) & hold_cids:
            drop["same_issue"] += 1
        else:
            seen_sha.add(k["sha_raw"])
            keep.append(i)
    print("leakage drops", drop, f"max cos train->holdout after drops {cos_all[keep].max():.3f}")

    pools = np.array([key[tr_ids[i]]["pool"] for i in keep])
    grades = np.array([key[tr_ids[i]]["grade"] for i in keep])
    y = np.array([rung(g) for g in grades])
    X = _features(Etr[keep], pools == "slab")
    groups = [key[tr_ids[i]]["comic_ids"][0] if key[tr_ids[i]]["comic_ids"] else f"pid{tr_ids[i]}" for i in keep]
    strat = [f"{p}{band_of(g, p)}" for p, g in zip(pools, grades)]
    folds = list(StratifiedGroupKFold(5, shuffle=True, random_state=1011).split(X, strat, groups))

    configs = [("ridge", a, bal) for a in (1, 10, 100, 300, 1000, 3000) for bal in (False, True)]
    configs += [("ordinal", w_, bal) for w_ in (1e-2, 1e-1, 0.3, 1.0, 3.0, 10.0) for bal in (False, True)]
    cv = []
    for head, hp, bal in configs:
        pred = np.zeros(len(y), dtype=int)
        for tri, vai in folds:
            mu, sd = X[tri].mean(0), X[tri].std(0) + 1e-6
            w = _weights(y[tri], pools[tri], bal)
            f = (_ridge_fit if head == "ridge" else _ordinal_fit)((X[tri] - mu) / sd, y[tri], w, hp)
            pred[vai] = f((X[vai] - mu) / sd)
        err = np.array([LADDER[p] for p in pred]) - grades
        cv.append({"head": head, "hp": hp, "balanced": bal,
                   "bb_within1": round(_band_balanced_hit(pred, grades, pools), 3),
                   "within1": round(float((np.abs(err) <= 1.0).mean()), 3),
                   "mae": round(float(np.abs(err).mean()), 3)})
        print(cv[-1], flush=True)
    best = {h: max((c for c in cv if c["head"] == h), key=lambda c: c["bb_within1"]) for h in ("ridge", "ordinal")}
    chosen = max(best.values(), key=lambda c: c["bb_within1"])

    # ---- refit on all training rows, score the holdout once per head
    mu, sd = X.mean(0), X.std(0) + 1e-6
    te_pool = np.array([hold[r]["pool"] for r in te_ids])
    Xte = (_features(Ete, te_pool == "slab") - mu) / sd
    out = {"encoder": enc, "strict_issue": strict_issue, "leak_drops": drop,
           "n_train": {"raw": int((pools == "raw").sum()), "slab": int((pools == "slab").sum())},
           "train_by_band": {p: [int(((pools == p) & (np.array([band_of(g, p) for g in grades], dtype=object) == b)).sum())
                                 for b in range(6)] for p in ("raw", "slab")},
           "cv": cv, "best": best, "chosen": chosen, "preds": {}}
    for h, c in best.items():
        f = (_ridge_fit if h == "ridge" else _ordinal_fit)((X - mu) / sd, y, _weights(y, pools, c["balanced"]), c["hp"])
        out["preds"][h] = {r: LADDER[int(p)] for r, p in zip(te_ids, f(Xte))}
    tag = f"{enc}{'_strict' if strict_issue else ''}"
    (wd / f"preds_{tag}.json").write_text(json.dumps(out, indent=1))
    timing(wd, f"fit_{tag}", time.time() - t0)
    print("chosen by CV:", chosen, f"{time.time() - t0:.0f}s")


def cmd_curve(wd: Path, enc: str, hp: float, balanced: bool) -> None:
    """Learning curve on training-set CV only (never the holdout): the ordinal head
    at a fixed hp, trained on 25/50/75/100% of each fold's training rows."""
    import numpy as np
    from sklearn.model_selection import StratifiedGroupKFold

    key = json.loads((wd / "train_key.json").read_text())
    ids = json.loads((wd / f"emb_{enc}_train_ids.json").read_text())
    E = np.load(wd / f"emb_{enc}_train.npy")
    pools = np.array([key[i]["pool"] for i in ids])
    grades = np.array([key[i]["grade"] for i in ids])
    y, X = np.array([rung(g) for g in grades]), _features(E, pools == "slab")
    groups = [key[i]["comic_ids"][0] if key[i]["comic_ids"] else f"pid{i}" for i in ids]
    strat = [f"{p}{band_of(g, p)}" for p, g in zip(pools, grades)]
    folds = list(StratifiedGroupKFold(5, shuffle=True, random_state=1011).split(X, strat, groups))
    rng = np.random.default_rng(0)
    for frac in (0.25, 0.5, 0.75, 1.0):
        pred = np.zeros(len(y), dtype=int)
        for tri, vai in folds:
            sub = rng.permutation(tri)[: int(len(tri) * frac)]
            mu, sd = X[sub].mean(0), X[sub].std(0) + 1e-6
            f = _ordinal_fit((X[sub] - mu) / sd, y[sub], _weights(y[sub], pools[sub], balanced), hp)
            pred[vai] = f((X[vai] - mu) / sd)
        print(f"train fraction {frac}: ~{int(len(y) * 0.8 * frac)} rows per fold, "
              f"band-balanced within-1.0 {_band_balanced_hit(pred, grades, pools):.3f}", flush=True)


# ------------------------------------------------------------------ metrics

def _qwk(p, t):
    from sklearn.metrics import cohen_kappa_score

    return cohen_kappa_score([rung(x) for x in p], [rung(x) for x in t], weights="quadratic",
                             labels=list(range(len(LADDER))))


def _row(pairs) -> str:
    if not pairs:
        return "| 0 | - | - | - | - | - | - |"
    p, t = [a for a, _ in pairs], [b for _, b in pairs]
    err = [a - b for a, b in pairs]
    ae = [abs(e) for e in err]
    varied = len(set(p)) > 1 and len(set(t)) > 1  # r and QWK are undefined on a constant side
    r = statistics.correlation(p, t) if varied else float("nan")
    k = _qwk(p, t) if varied else float("nan")
    return (f"| {len(pairs)} | {statistics.mean(ae):.2f} | {sum(a <= 1.0 for a in ae) / len(ae):.0%} | "
            f"{sum(a <= 0.5 for a in ae) / len(ae):.0%} | {statistics.mean(err):+.2f} | {r:.2f} | {k:.2f} |")


def cmd_metrics(wd: Path, hd: Path, tag: str, head: str | None) -> None:
    res = json.loads((wd / f"preds_{tag}.json").read_text())
    head = head or res["chosen"]["head"]
    hold = json.loads((hd / "key.json").read_text())
    haiku = {r["product_id"]: r["pred"] for r in json.loads((hd / "joined.json").read_text())}
    preds = res["preds"][head]
    rows = [{**hold[r], "pred": p, "haiku": haiku.get(hold[r]["product_id"])} for r, p in preds.items()]
    assert len(rows) == 150 and all(r["haiku"] is not None for r in rows)
    print(f"encoder {res['encoder']} head {head} (CV-chosen: {res['chosen']}) n_train {res['n_train']}")
    print(f"train by band {res['train_by_band']}; leak drops {res['leak_drops']}\n")
    hdr = "| Cut | n | MAE | within ±1.0 | within ±0.5 | bias | Pearson r | QWK |\n|---|---|---|---|---|---|---|---|"
    print(hdr)
    for name, sel in (("Raw", "raw"), ("Slab", "slab"), ("Combined", None)):
        print(f"| {name} " + _row([(r["pred"], r["grade"]) for r in rows if sel in (None, r["pool"])]))
    for pool, bands in (("raw", RAW_BANDS), ("slab", SLAB_BANDS)):
        print(f"\n{pool} by band\n" + hdr)
        for b, (lo, hi) in enumerate(bands):
            print(f"| {lo}-{hi} " + _row([(r["pred"], r["grade"]) for r in rows if r["pool"] == pool and r["band"] == b]))
    for sel in ("raw", "slab", None):
        sub = [r for r in rows if sel in (None, r["pool"])]
        t = [r["grade"] for r in sub]
        med = statistics.median(t)
        print(f"{sel or 'combined'}: median guess {med} -> within ±1.0 {sum(abs(med - x) <= 1.0 for x in t) / len(t):.0%}, "
              f"MAE {statistics.mean(abs(med - x) for x in t):.2f}; haiku " + _row([(r['haiku'], r['grade']) for r in sub]))
    m_hit = [abs(r["pred"] - r["grade"]) <= 1.0 for r in rows]
    h_hit = [abs(r["haiku"] - r["grade"]) <= 1.0 for r in rows]
    print(f"\npaired: model fixes {sum(m and not h for m, h in zip(m_hit, h_hit))} of Haiku's "
          f"{sum(not h for h in h_hit)} misses; breaks {sum(h and not m for m, h in zip(m_hit, h_hit))} of Haiku's hits; "
          f"both miss {sum(not m and not h for m, h in zip(m_hit, h_hit))}")
    p = [r["pred"] for r in rows]
    print(f"pred spread min {min(p)} max {max(p)} stdev {statistics.pstdev(p):.2f} "
          f"(true {statistics.pstdev([r['grade'] for r in rows]):.2f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["sample", "fetch", "embed", "fit", "curve", "metrics"])
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--holdout", required=True, type=Path)
    ap.add_argument("--encoder", choices=sorted(ENCODERS), default="b32")
    ap.add_argument("--strict-issue", action="store_true",
                    help="also drop training comps that share a comic_id with any holdout comp")
    ap.add_argument("--tag", help="metrics: preds_<tag>.json (default: the encoder)")
    ap.add_argument("--head", choices=["ridge", "ordinal"], help="metrics: override the CV-chosen head")
    ap.add_argument("--hp", type=float, default=10.0, help="curve: ordinal weight decay")
    ap.add_argument("--balanced", action="store_true", help="curve: class-balanced weights")
    a = ap.parse_args()
    a.workdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if a.step == "sample":
        cmd_sample(a.workdir, a.holdout)
        timing(a.workdir, "sample", time.time() - t0)
    elif a.step == "fetch":
        cmd_fetch(a.workdir)
    elif a.step == "embed":
        cmd_embed(a.workdir, a.holdout, a.encoder)
    elif a.step == "fit":
        cmd_fit(a.workdir, a.holdout, a.encoder, a.strict_issue)
    elif a.step == "curve":
        cmd_curve(a.workdir, a.encoder, a.hp, a.balanced)
    else:
        cmd_metrics(a.workdir, a.holdout, a.tag or a.encoder, a.head)


if __name__ == "__main__":
    main()
