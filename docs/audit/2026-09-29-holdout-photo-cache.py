#!/usr/bin/env python3
"""Cache full-resolution photos for the BUI-1006/BUI-1011 grader holdout (BUI-1012).

BUI-1006 and BUI-1011 both scored a photo grader on the same 150 sold comps
(100 raw, 50 CGC/CBCS slabs). The key file, the 150 prepped 768px images, and
both runs' predictions live only in a session scratch directory macOS
cleans, and the listings' own eBay photos disappear ~90 days after the sale
(BUI-1011). This script is the durable half of BUI-1012: given the already
-copied `key.json` (150 entries: random 8-hex id -> {product_id, title,
grade, pool, sold_date, band}) at `<workdir>/bui-1006/key.json`, it re-fetches
every one of those 150 listings through the Browse API and downloads EVERY
image (main + additionalImages) at native resolution -- no downscale, no
crop, unlike BUI-1006's single 768px front cover -- so a later multi-photo
measurement has real inputs to work from.

Read-only against the comics DB (not used at all here -- the key file already
carries every label BUI-1006 sampled). No ledger writes. ~150 Browse calls.

Usage:
    uv run --python 3.12 --with requests python \\
        docs/audit/2026-09-29-holdout-photo-cache.py --workdir ~/comic-grader-fixtures/bui-1006-holdout

Layout:
    <workdir>/bui-1006/key.json        input (already copied, see BUI-1012)
    <workdir>/full/<product_id>/       img-01.jpg, img-02.jpg, ... + item.json
    <workdir>/manifest.json            per-comp result + summary (written by cmd_manifest,
                                        also refreshed at the end of cmd_fetch)

Resumable: a listing directory counts as done only when it has an item.json
whose recorded image_count matches the number of non-empty img-NN.jpg files
actually on disk (see _is_complete) -- a crash mid-download must not look
resumable-complete, so an incomplete directory is wiped and retried from
scratch rather than trusted.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

EBAY_SRC = Path(__file__).resolve().parents[2] / "apps" / "ebay" / "src"
MAX_WORKERS = 4
DOWNLOAD_TIMEOUT = 30
ITEM_FIELDS = (
    "title",
    "condition",
    "conditionDescription",
    "description",
    "itemEndDate",
    "legacyItemId",
    "itemId",
)


def _load_key(workdir: Path) -> dict:
    key_path = workdir / "bui-1006" / "key.json"
    if not key_path.exists():
        sys.exit(
            f"error: {key_path} not found -- copy the bui-1006 holdout artifacts "
            "(key.json, images/, joined.json, rejected.json) into <workdir>/bui-1006/ "
            "before running this script (see BUI-1012)."
        )
    return json.loads(key_path.read_text())


def _is_complete(item_dir: Path) -> bool:
    """True only when item.json exists, names a positive image_count, and
    every img-NN.jpg it implies is present with nonzero size on disk.

    A partial run (killed mid-download) never writes item.json (see
    _fetch_one), so the common case is just "item.json missing". This also
    guards the rarer case of a manually-truncated or hand-edited directory.
    """
    item_json = item_dir / "item.json"
    if not item_json.exists():
        return False
    try:
        meta = json.loads(item_json.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    n = meta.get("image_count")
    if not isinstance(n, int) or n < 1:
        return False
    for i in range(1, n + 1):
        f = item_dir / f"img-{i:02d}.jpg"
        if not f.exists() or f.stat().st_size == 0:
            return False
    return True


class TokenBox:
    """Thread-safe holder for the current OAuth app token.

    Multiple worker threads share one token; on a 401 any of them can force
    a single refresh without racing each other into redundant token requests
    (each caller re-reads .token under the lock after refresh() returns, so a
    thread that loses the race to refresh first still gets the fresh token,
    not a second refresh of its own).
    """

    def __init__(self, cid: str, secret: str, base: str, token: str):
        self._cid, self._secret, self._base = cid, secret, base
        self._lock = threading.Lock()
        self._token = token
        self._last_refreshed_for: str | None = None

    def get(self) -> str:
        with self._lock:
            return self._token

    def refresh_after(self, stale_token: str) -> str:
        """Refresh unless another thread already refreshed past stale_token."""
        from ebay_fetch import get_token  # local import: sys.path set up in main()

        with self._lock:
            if self._token != stale_token:
                return self._token  # someone else already refreshed
            self._token = get_token(self._cid, self._secret, self._base, force_refresh=True)
            return self._token


def _fetch_one(pid: str, base: str, token_box: TokenBox, full_dir: Path) -> dict:
    """Fetch one listing and download every image at native resolution.

    Returns a result dict always containing "product_id"; on success it also
    has "image_count", "total_bytes", "id_match", "title_match", "title"
    (echoing the response's own title, for the manifest's title_match
    check against the ledger); on failure it has "error" and, when known,
    "status".
    """
    from ebay_fetch import fetch_item_with_status  # local import: sys.path set up in main()

    item_dir = full_dir / pid
    token = token_box.get()
    data, status = fetch_item_with_status(pid, token, base, retries=1)
    if data is None and status == 401:
        token = token_box.refresh_after(token)
        data, status = fetch_item_with_status(pid, token, base, retries=1)
    if data is None:
        return {"product_id": pid, "error": "fetch_failed", "status": status}

    try:
        urls = []
        if "image" in data:
            urls.append(data["image"]["imageUrl"])
        for ai in data.get("additionalImages", []) or []:
            urls.append(ai["imageUrl"])
    except (KeyError, TypeError) as e:
        return {"product_id": pid, "error": f"malformed_image_data ({e})", "status": status}

    if not urls:
        return {"product_id": pid, "error": "no_images", "status": status}

    # BUI-300-style hygiene (grade_photos.py's download_listing): wipe any
    # stale partial directory from a prior crashed attempt before writing —
    # a re-run must never mix old and new images under the same NN index.
    if item_dir.exists():
        shutil.rmtree(item_dir)
    item_dir.mkdir(parents=True)

    total_bytes = 0
    try:
        for i, url in enumerate(urls, 1):
            resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
            dest = item_dir / f"img-{i:02d}.jpg"
            dest.write_bytes(resp.content)
            total_bytes += len(resp.content)
    except requests.exceptions.RequestException as e:
        # A failed image must not leave this listing looking complete: no
        # item.json is written, so _is_complete() reports False and the next
        # run wipes item_dir and retries the whole listing from scratch.
        return {"product_id": pid, "error": f"image_download_failed ({e})", "status": status}

    resp_title = data.get("title")
    resp_legacy_id = data.get("legacyItemId")
    item_json = {k: data.get(k) for k in ITEM_FIELDS}
    item_json["image_count"] = len(urls)
    item_json["image_urls"] = urls
    (item_dir / "item.json").write_text(json.dumps(item_json, indent=1))

    return {
        "product_id": pid,
        "image_count": len(urls),
        "total_bytes": total_bytes,
        "id_match": resp_legacy_id == pid,
        "title": resp_title,
    }


def cmd_fetch(workdir: Path, max_workers: int) -> None:
    sys.path.insert(0, str(EBAY_SRC))
    from ebay_fetch import get_token, load_config

    key = _load_key(workdir)
    full_dir = workdir / "full"
    full_dir.mkdir(parents=True, exist_ok=True)

    todo = {pid: comp for pid, comp in ((c["product_id"], c) for c in key.values())}
    already_done = {pid for pid in todo if _is_complete(full_dir / pid)}
    pending = [pid for pid in todo if pid not in already_done]
    print(f"{len(key)} comps, {len(already_done)} already cached, {len(pending)} to fetch", flush=True)

    manifest = _load_manifest(workdir)
    results_by_pid = {r["product_id"]: r for r in manifest.get("comps", [])}

    if pending:
        cid, secret, base = load_config()
        token_box = TokenBox(cid, secret, base, get_token(cid, secret, base))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, pid, base, token_box, full_dir): pid for pid in pending}
            for n, fut in enumerate(as_completed(futures), 1):
                pid = futures[fut]
                result = fut.result()
                results_by_pid[pid] = result
                status = "ok" if "error" not in result else f"FAILED ({result['error']})"
                print(f"[{n}/{len(pending)}] {pid}: {status}", flush=True)

    # Fold in comps that were already complete from a prior run but have no
    # manifest entry yet (e.g. the manifest.json itself was lost/regenerated).
    for pid in already_done:
        if pid not in results_by_pid:
            meta = json.loads((full_dir / pid / "item.json").read_text())
            total_bytes = sum((full_dir / pid / f"img-{i:02d}.jpg").stat().st_size
                               for i in range(1, meta["image_count"] + 1))
            results_by_pid[pid] = {
                "product_id": pid,
                "image_count": meta["image_count"],
                "total_bytes": total_bytes,
                "id_match": meta.get("legacyItemId") == pid,
                "title": meta.get("title"),
            }

    _write_manifest(workdir, key, results_by_pid)


def _load_manifest(workdir: Path) -> dict:
    p = workdir / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"comps": []}


def _write_manifest(workdir: Path, key: dict, results_by_pid: dict) -> None:
    comps = []
    fetched_per_pool = {"raw": 0, "slab": 0}
    failed_per_pool = {"raw": 0, "slab": 0}
    image_count_histogram: dict[str, int] = {}

    for rid, comp in key.items():
        pid = comp["product_id"]
        r = results_by_pid.get(pid)
        row = {
            "product_id": pid,
            "rid": rid,
            "pool": comp["pool"],
            "grade": comp["grade"],
            "band": comp["band"],
            "sold_date": comp["sold_date"],
            "title": comp["title"],
        }
        if r is None:
            row["error"] = "not_attempted"
            failed_per_pool[comp["pool"]] += 1
        elif "error" in r:
            row["error"] = r["error"]
            if "status" in r:
                row["status"] = r["status"]
            failed_per_pool[comp["pool"]] += 1
        else:
            row["image_count"] = r["image_count"]
            row["total_bytes"] = r["total_bytes"]
            row["id_match"] = r["id_match"]
            row["title_match"] = r.get("title") == comp["title"]
            fetched_per_pool[comp["pool"]] += 1
            key_hist = str(r["image_count"])
            image_count_histogram[key_hist] = image_count_histogram.get(key_hist, 0) + 1
        comps.append(row)

    total_fetched = sum(fetched_per_pool.values())
    total_failed = sum(failed_per_pool.values())
    manifest = {
        "comps": comps,
        "summary": {
            "fetched": {**fetched_per_pool, "total": total_fetched},
            "failed": {**failed_per_pool, "total": total_failed},
            "total_comps": len(key),
            "image_count_histogram": dict(sorted(image_count_histogram.items(), key=lambda kv: int(kv[0]))),
            "total_bytes_on_disk": sum(r.get("total_bytes", 0) for r in comps),
        },
    }
    (workdir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    s = manifest["summary"]
    print(f"\nmanifest: fetched {s['fetched']} failed {s['failed']} "
          f"(total {s['fetched']['total'] + s['failed']['total']} of {s['total_comps']})")
    print(f"image_count_histogram: {s['image_count_histogram']}")
    print(f"total_bytes_on_disk: {s['total_bytes_on_disk']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True, type=lambda p: Path(p).expanduser())
    ap.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    a = ap.parse_args()
    a.workdir.mkdir(parents=True, exist_ok=True)
    cmd_fetch(a.workdir, a.max_workers)


if __name__ == "__main__":
    main()
