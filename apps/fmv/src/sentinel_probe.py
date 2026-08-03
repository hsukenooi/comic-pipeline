"""``comic-fmv --sentinel-probe``: calibration probes for the sold-comps
pipeline (BUI-603).

This is NOT an FMV pricing run. It shells out to the same ``ebay-sold-comps``
binary a real FMV batch uses (apps/ebay), for a fixed, known list of books
chosen so the "right answer" is already known:

  * SENTINEL_BOOKS — 2-3 deep, liquid, decades-old keys. A genuine market
    never reduces their comp count to zero, so ``n=0`` (or a fetch error, or
    a wild swing in the priced median) can only mean the INSTRUMENT broke —
    the provider, the cache, the query construction, or the parsing — not
    that the book stopped selling. See each entry's ``measured_pool_depth``
    for the corpus evidence this claim rests on.
  * NEGATIVE_CONTROL_BOOK — a query engineered to match nothing, ever. If it
    comes back with comps, the MATCHER broke open: it is now matching
    listings that have nothing to do with the query.

fetch-err and genuine-zero look identical in the raw output (BUI-565/570 is
exactly a per-book crash that surfaced as a clean n=0) — sentinels sidestep
that whole trap class because, for these specific books, the expected
answer is known in advance. This module deliberately does NOT generalize
that into a pool-shape heuristic for arbitrary books (BUI-578/582/592/590
already falsified four such heuristics on measurement) — it only ever
judges these five fixed, hand-picked queries against their own history.

Hard constraint: results here are pure calibration and are NEVER written to
the fmv/comics DB. This module has no code path that can reach that write —
it never imports ``fmv_runner`` (the only place ``/api/comics`` is POSTed
from) and never constructs that URL itself. The only server call it makes
is the BUI-602 heartbeat ping below, a different endpoint entirely, fired
only after every check below has already passed.

Run this weekly, not on every ``/comic:fmv`` invocation — see
``run_sentinel_probe``'s docstring for the cadence rationale (provider
request budget, BUI-565/570).
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

import fmv_math


EBAY_SOLD_COMPS_BIN = "ebay-sold-comps"
HEARTBEAT_JOB_NAME = "sentinel-probe"

# ─── Sentinel selection (BUI-603) ─────────────────────────────────────────────
#
# All three are Marvel/DC back-issue "keys" — decades of sustained collector
# demand, never speculative/modern (a hot recent book can legitimately swing
# 50%+ in a week on real news, which would make it a bad instrument for a
# PRICE-jump check). Depths below are unique, hard_exclude-surviving raw comps
# measured directly against the cached corpus at
# ~/.cache/ebay-sold-comps/ (2026-06 snapshot; see the BUI-603 commit for the
# measurement script) — NOT read from memory. Each book measured comfortably
# above SENTINEL_MIN_N (10) even from a single stale snapshot, which is the
# basis for treating n=0 as impossible absent an instrument failure.
SENTINEL_BOOKS: list[dict] = [
    {
        # First full appearance of Venom. One of the most consistently
        # liquid modern keys in the hobby.
        "title": "Amazing Spider-Man",
        "issue": "300",
        "year": 1988,
        "publisher": "marvel",
        "target_grade": 8.5,
        "measured_pool_depth": 85,  # unique valid raw comps, 2 cached query snapshots
    },
    {
        # The classic "Laughing Fish" Joker cover — a top Bronze Age DC key.
        "title": "Batman",
        "issue": "251",
        "year": 1973,
        "publisher": "dc",
        "target_grade": 7.0,
        "measured_pool_depth": 62,  # unique valid raw comps, 2 cached query snapshots
    },
    {
        # Cap's title resumes its original numbering — a Silver Age key with
        # deep sustained demand.
        "title": "Captain America",
        "issue": "100",
        "year": 1968,
        "publisher": "marvel",
        "target_grade": 6.5,
        "measured_pool_depth": 88,  # unique valid raw comps, 3 cached query snapshots
    },
]

# A query built to match nothing on eBay, ever. The nonsense title alone
# would already be extremely unlikely to collide with a real future comic;
# the absurd issue number (no American comic has printed anywhere near six
# digits — Detective Comics, the longest continuously running one, is still
# in the low thousands) makes a genuine future collision effectively
# impossible even if some publisher ever reused the title text.
NEGATIVE_CONTROL_BOOK: dict = {
    "title": "Zzyzxblorqennial Vortex Chronicles",
    "issue": "999999",
    "year": None,
    "publisher": None,
}

# BUI-603: floor below which a sentinel's RAW comp count (comp_count_total —
# the same count BUI-536's fetch-err guard keys off, before any grade-window
# trim) signals degradation on its own, distinct from the n=0 hard failure.
# Every sentinel above measured 62-88 unique raw comps from a single cached
# snapshot; 10 sits far below all three readings while staying safely above
# the single-digit counts a half-broken query (e.g. one tier silently
# failing) can still limp to.
SENTINEL_MIN_N = 10

# BUI-603: "wild price jump" bounds on (this run's grade-windowed, IQR-trimmed
# median) / (last known-good median for the same book+grade). Grounded in the
# same corpus: fmv_math.cv() (stdev/median) on these three sentinels' own
# grade-windowed pools measured 0.07-0.56 — ordinary, HIGH-to-MEDIUM-confidence
# dispersion by fmv_math.confidence_label()'s own bands (25%-45% cv is
# "normal" there). A ratio outside [0.5, 2.0] is a 100%+ swing: 2-4x the
# widest healthy CV measured, and — since a MIN_NARROW_POOL-sized pool's
# median has a standard error well under its raw CV — a multi-sigma event
# under ordinary resampling noise. A jump that large is not "the market
# moved" for a decades-old key; it means the wrong thing got priced (wrong
# grade window, a slab/lot/statue that slipped past hard_exclude, or a
# provider serving garbage).
PRICE_JUMP_UPPER_RATIO = 2.0
PRICE_JUMP_LOWER_RATIO = 0.5

# BUI-184-style subprocess budget: a small, fixed batch (3 sentinels + 1
# negative control), so a fixed ceiling is fine — no need for fmv_runner's
# per-book scaling.
_SUBPROCESS_TIMEOUT_SEC = 300


# ─── Baseline persistence (NOT the fmv DB — see module docstring) ─────────────

def _state_dir() -> Path:
    """Overridable via COMIC_FMV_SENTINEL_STATE_DIR (tests; also lets a
    non-default disk be used, mirroring EBAY_SOLD_COMPS_CAPTURE_DIR)."""
    return Path(
        os.environ.get("COMIC_FMV_SENTINEL_STATE_DIR")
        or (Path.home() / ".cache" / "comic-fmv-sentinel")
    )


def _baseline_path() -> Path:
    return _state_dir() / "baseline.json"


def _book_key(book: dict) -> str:
    """Include target_grade: if a sentinel's grade is ever retuned in code,
    the OLD baseline (priced at the old grade) would otherwise be compared
    against a new-grade median on the very next deploy — an apples-to-oranges
    reading that could false-positive as PRICE_JUMP. Folding grade into the
    key means a retune just reseeds a fresh baseline (fail-open) instead."""
    return f"{book['title']}|{book['issue']}|{book.get('year')}|{book.get('target_grade')}"


def _load_baselines() -> dict:
    path = _baseline_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_baselines(data: dict) -> None:
    """Atomic tmp-write + rename, self-contained (no cross-package import of
    apps/ebay's ebay_fetch.atomic_write_json — see the module docstring on
    why comic-fmv shells out to, rather than imports, apps/ebay)."""
    path = _baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


# ─── Fetch (shell out to ebay-sold-comps — see module docstring) ──────────────

def _sentinel_batch() -> list[dict]:
    """The fixed probe batch: sentinels first, negative control last, each
    tagged a stable ``_req_id`` so results map back by identity, never
    position (same BUI-174/187 discipline fmv_runner uses)."""
    books = SENTINEL_BOOKS + [NEGATIVE_CONTROL_BOOK]
    return [
        {
            "title": b["title"],
            "issue": b["issue"],
            "year": b.get("year"),
            "publisher": b.get("publisher"),
            "grade": b.get("target_grade"),
            "_req_id": i,
        }
        for i, b in enumerate(books)
    ]


def _fetch_batch(payload: list[dict]) -> list[dict] | None:
    """Subprocess to ebay-sold-comps. Returns the parsed per-book result
    list (any order — map by ``_req_id``), or None if the probe itself could
    not run (missing binary, timeout, non-zero exit, unreadable/unparseable
    output). Deliberately NOT fmv_runner._fetch_comps and does not import
    fmv_runner — see the module docstring's hard-constraint note."""
    if shutil.which(EBAY_SOLD_COMPS_BIN) is None:
        print(
            f"sentinel-probe: '{EBAY_SOLD_COMPS_BIN}' not found on PATH. "
            "Install apps/ebay (see scripts/install.sh).",
            file=sys.stderr,
        )
        return None

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as ftmp:
        json.dump(payload, ftmp)
        in_path = ftmp.name
    out_path = in_path + ".out.json"

    try:
        cmd = [EBAY_SOLD_COMPS_BIN, "--batch", in_path, "--out", out_path, "--quiet"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            print(
                f"sentinel-probe: {EBAY_SOLD_COMPS_BIN} timed out after "
                f"{_SUBPROCESS_TIMEOUT_SEC}s.",
                file=sys.stderr,
            )
            return None
        if result.returncode != 0:
            print(
                f"sentinel-probe: {EBAY_SOLD_COMPS_BIN} failed "
                f"(exit {result.returncode}):\n{result.stderr}",
                file=sys.stderr,
            )
            return None
        try:
            raw = Path(out_path).read_text()
        except OSError as e:
            print(f"sentinel-probe: could not read output: {e}", file=sys.stderr)
            return None
        if not raw.strip():
            print(
                f"sentinel-probe: {EBAY_SOLD_COMPS_BIN} exited 0 but wrote no output.",
                file=sys.stderr,
            )
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"sentinel-probe: unparseable output: {e}", file=sys.stderr)
            return None
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ─── Pool math (fmv_math is pure/no-I/O — see fmv_math.py's own docstring) ────

def _priced_median(comps: list[dict], target_grade: float) -> tuple[float, int, float] | None:
    """Grade-window (fmv_math.build_pool) + IQR-trim + median, matching the
    same steps compute_fmv itself runs. None if no comp in the pool carries a
    parsed grade within fmv_math.MAX_GRADE_WINDOW."""
    pool, window = fmv_math.build_pool(comps, target_grade=target_grade)
    if not pool:
        return None
    prices = sorted(c["price"] for c in pool)
    trimmed = fmv_math.iqr_trim(prices) if len(prices) >= 3 else prices
    if not trimmed:
        return None
    return statistics.median(trimmed), len(trimmed), window


# ─── Per-book verdicts ─────────────────────────────────────────────────────────

class _Verdict:
    OK = "OK"
    OK_BASELINE_SEEDED = "OK (baseline seeded)"
    ZERO_COMPS = "ALERT: n=0"
    FETCH_ERROR = "ALERT: fetch error"
    DEGRADED_POOL = "ALERT: pool below floor"
    NO_GRADEABLE_COMPS = "ALERT: no gradeable comps"
    PRICE_JUMP = "ALERT: wild price jump"
    MATCHER_BROKE_OPEN = "ALERT: negative control matched"
    EVAL_ERROR = "ALERT: evaluation error"


def _safe_check(label: str, fn, *args) -> dict:
    """Run a single book's check, converting any unexpected exception (a
    malformed comp — e.g. a missing 'price' key — reaching this far would
    otherwise mean an upstream schema change) into a failing report instead
    of crashing the whole batch or, worse, silently reading as healthy. Every
    OTHER book's report must still print even if one book's evaluation blows
    up (# noqa-style broad except, same convention as sold_comps.py's own
    BUI-537 per-book exception boundary — the whole point of a probe is to
    surface breakage, not become another silent-failure site itself)."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — see docstring: must not crash the batch
        return {"label": label, "verdict": _Verdict.EVAL_ERROR, "n": -1,
                "detail": f"unexpected error evaluating this book: {e!r}",
                "pass": False}


def _check_sentinel(book: dict, result: dict, baselines: dict) -> dict:
    """Evaluate one sentinel's result. Returns a report dict; never raises.
    Mutates `baselines` in place ONLY when this book's read is healthy — a
    bad read must never become the next run's baseline (see module docstring
    on ratio math)."""
    label = f"{book['title']} #{book['issue']}"
    comps = result.get("comps", [])
    n = len(comps)

    if result.get("error") is not None:
        return {"label": label, "verdict": _Verdict.FETCH_ERROR, "n": n,
                "detail": str(result["error"]), "pass": False}
    if n == 0:
        return {"label": label, "verdict": _Verdict.ZERO_COMPS, "n": 0,
                "detail": "measured_pool_depth was "
                          f"{book['measured_pool_depth']}; got 0", "pass": False}
    if n < SENTINEL_MIN_N:
        return {"label": label, "verdict": _Verdict.DEGRADED_POOL, "n": n,
                "detail": f"n={n} < floor {SENTINEL_MIN_N} "
                          f"(measured_pool_depth={book['measured_pool_depth']})",
                "pass": False}

    priced = _priced_median(comps, book["target_grade"])
    if priced is None:
        return {"label": label, "verdict": _Verdict.NO_GRADEABLE_COMPS, "n": n,
                "detail": f"n={n} raw comps but none carry a parsed grade "
                          f"within ±{fmv_math.MAX_GRADE_WINDOW} of "
                          f"{book['target_grade']}", "pass": False}
    median, pool_n, window = priced

    key = _book_key(book)
    prior = baselines.get(key)
    if prior is None:
        baselines[key] = {"median": median, "pool_n": pool_n}
        return {"label": label, "verdict": _Verdict.OK_BASELINE_SEEDED, "n": n,
                "detail": f"median=${median:.2f} (pool_n={pool_n}, "
                          f"window=±{window}); no prior baseline to compare",
                "pass": True}

    baseline_median = prior["median"]
    ratio = median / baseline_median if baseline_median else float("inf")
    if ratio > PRICE_JUMP_UPPER_RATIO or ratio < PRICE_JUMP_LOWER_RATIO:
        return {"label": label, "verdict": _Verdict.PRICE_JUMP, "n": n,
                "detail": f"median=${median:.2f} vs baseline ${baseline_median:.2f} "
                          f"(ratio={ratio:.2f}x, outside "
                          f"[{PRICE_JUMP_LOWER_RATIO}, {PRICE_JUMP_UPPER_RATIO}])",
                "pass": False}

    # Healthy read — roll the baseline forward (see module docstring: only a
    # PASSING run may update it).
    baselines[key] = {"median": median, "pool_n": pool_n}
    return {"label": label, "verdict": _Verdict.OK, "n": n,
            "detail": f"median=${median:.2f} (pool_n={pool_n}, window=±{window}, "
                      f"ratio={ratio:.2f}x vs baseline)", "pass": True}


def _check_negative_control(result: dict) -> dict:
    label = f"{NEGATIVE_CONTROL_BOOK['title']} #{NEGATIVE_CONTROL_BOOK['issue']} (negative control)"
    comps = result.get("comps", [])
    n = len(comps)
    if n > 0:
        sample_titles = [c.get("title", "?") for c in comps[:3]]
        return {"label": label, "verdict": _Verdict.MATCHER_BROKE_OPEN, "n": n,
                "detail": f"expected 0, got {n}; sample matches: {sample_titles}",
                "pass": False}
    note = ""
    if result.get("error") is not None:
        note = f" (fetch also errored: {result['error']}; irrelevant here — 0 is 0)"
    return {"label": label, "verdict": _Verdict.OK, "n": 0,
            "detail": f"0 comps, as expected{note}", "pass": True}


# ─── Heartbeat (BUI-602 surface — no second alerting surface invented) ────────

def _ping_heartbeat(server_url: str | None) -> None:
    """Best-effort POST /api/heartbeat/sentinel-probe. Never raises and never
    changes the probe's exit code — the probe's own report/exit-code IS the
    alert surface; the heartbeat is the staleness backstop on top of it.

    BUI-624 registered 'sentinel-probe' in gixen_overlay.db.JOB_CONTRACTS, so
    this no longer 404s against a current server; the BUI-603 note that it
    would has been removed rather than left to mislead. The 404 branch below
    stays anyway — it is now a package-skew signal (an older overlay deployed
    against a newer comic-fmv), and it must stay non-fatal for the same reason
    it always was: the probe's own exit code is the alert surface, and a
    heartbeat that could fail the probe would invert their roles.

    Called ONLY from `run_sentinel_probe`'s all-pass branch. Exit 1 (ran, found
    the pipeline miscalibrated) and exit 2 (could not complete) both skip it —
    see the `success` field of the JOB_CONTRACTS entry for why the stricter
    definition is deliberate here.
    """
    if not server_url:
        print(
            "sentinel-probe: no comics-server URL (COMICS_SERVER_URL / "
            "--server-url) — skipping the heartbeat ping.",
            file=sys.stderr,
        )
        return
    try:
        resp = requests.post(
            f"{server_url}/api/heartbeat/{HEARTBEAT_JOB_NAME}", timeout=10,
        )
        if resp.status_code == 404:
            print(
                f"sentinel-probe: heartbeat ping 404 — {HEARTBEAT_JOB_NAME!r} is "
                "not in this server's JOB_CONTRACTS. BUI-624 added it, so this "
                "means the deployed overlay predates that change; re-deploy the "
                "comics server (./scripts/deploy.sh).",
                file=sys.stderr,
            )
            return
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"sentinel-probe: heartbeat ping failed (non-fatal): {e}", file=sys.stderr)


# ─── Entry point ────────────────────────────────────────────────────────────

def run_sentinel_probe(*, server_url: str | None = None) -> int:
    """Run the sentinel + negative-control batch once and report.

    Cadence (BUI-603): call this WEEKLY (e.g. from a scheduled agent or a
    cron-style skill), never per-/comic:fmv-invocation — each run spends real
    provider request budget on top of whatever real FMV batches already ran
    that day, and comic-fmv has no --max-workers to bound that spend
    (BUI-565/570). Uses the ebay-sold-comps response cache like any other
    caller (no --force), so a sentinel that happens to overlap a real recent
    FMV query costs nothing extra.

    Returns:
      0 — every sentinel and the negative control passed.
      1 — the probe ran to completion and at least one check failed (the
          alarm this whole module exists to raise).
      2 — the probe itself could not complete (binary missing, subprocess
          timeout/crash, or an ebay-sold-comps result/identity mismatch) —
          distinct from 1 so a caller doesn't mistake "couldn't check" for
          "checked, and it's broken".
    """
    payload = _sentinel_batch()
    results = _fetch_batch(payload)
    if results is None:
        return 2

    by_id: dict[int, dict] = {}
    for r in results:
        rid = (r.get("input") or {}).get("_req_id")
        if rid in by_id:
            print(f"sentinel-probe: duplicate result id {rid!r}; refusing to "
                  "map results positionally.", file=sys.stderr)
            return 2
        by_id[rid] = r
    sent_ids = {b["_req_id"] for b in payload}
    if set(by_id) != sent_ids or len(results) != len(payload):
        print(
            f"sentinel-probe: result/identity mismatch — sent {len(payload)}, "
            f"got {len(results)}. Refusing to report against mismatched books.",
            file=sys.stderr,
        )
        return 2

    baselines = _load_baselines()
    reports = [
        _safe_check(f"{book['title']} #{book['issue']}",
                   _check_sentinel, book, by_id[i], baselines)
        for i, book in enumerate(SENTINEL_BOOKS)
    ]
    reports.append(_safe_check(
        f"{NEGATIVE_CONTROL_BOOK['title']} #{NEGATIVE_CONTROL_BOOK['issue']} "
        "(negative control)",
        _check_negative_control, by_id[len(SENTINEL_BOOKS)],
    ))

    print("comic-fmv sentinel probe (BUI-603) — calibration only, nothing written to FMV")
    for rep in reports:
        print(f"  [{rep['verdict']}] {rep['label']}: n={rep['n']} — {rep['detail']}")

    # `baselines` was only mutated in place for books whose OWN read passed
    # (_check_sentinel returns before touching it on any failing verdict) —
    # so persisting it here is safe even on a partial failure: a failing
    # book's entry is exactly what _load_baselines() returned, untouched: a
    # bad read can never become that book's own next point of comparison,
    # while a healthy sentinel's update is not held hostage by an unrelated
    # book (or the negative control) failing in the same run.
    _save_baselines(baselines)

    all_pass = all(rep["pass"] for rep in reports)
    if all_pass:
        print("sentinel-probe: all checks passed.")
        _ping_heartbeat(server_url)
        return 0

    print("sentinel-probe: ALERT — one or more checks failed (see above).",
          file=sys.stderr)
    return 1
