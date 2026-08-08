#!/usr/bin/env python3
"""ebay-sold-comps: fetch eBay sold listings for a comic.

Runs each query through a provider chain (BUI-545): sold-comps.com is the
default primary — eBay's 2026-07-23 login wall killed SerpApi's logged-out
sold engine indefinitely — with SerpApi's eBay engine (show_only=Sold) as the
fallback tier, failing over per query on error or a tripped breaker. Order is
configurable via EBAY_SOLD_COMPS_PROVIDERS. Caches responses per provider,
dedupes by product_id, applies hard-excludes, parses grades, and returns
clean comp lists. Consumed by comic-pipeline-fmv (apps/fmv) to compute fair
market value. Every raw provider response that HAS A BODY is also appended,
unmodified, to an append-only capture file (BUI-614) — a hedge against
losing sold-comps history before a real comps ledger (BUI-610) exists; see
CAPTURE_DIR/CAPTURE_PATH and _capture_raw_response(). Since BUI-628, that
includes a response that fails our own shape validation (a dropped
LH_Sold=1, a sold-comps.com shape violation) — tagged with the validation
outcome — because provider drift is exactly what tier 0 exists to make
diagnosable, and a network error with no body still captures nothing.
BUI-628 also rotates CAPTURE_PATH by size (never by age, never pruned) so
it cannot grow into a full disk.

Why this lives in apps/ebay (alongside ebay-fetch):
    All eBay data fetching — live (Browse API, ebay_fetch.py) and sold
    (SerpApi, this file) — belongs in the same app. Comic-specific FMV math
    and DB upsert live in apps/fmv; this file is the eBay side of that pipeline.
"""

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

import comic_identity
from ebay_fetch import RetryExhausted, atomic_write_json, retry_request


def _version_string() -> str:
    """BUI-314: staleness signal for a `uv tool install`ed binary.

    `_ebay_build_stamp` is generated at build time by hatch_build.py from the
    git HEAD of the source tree the wheel was built from; it's absent when
    running from an unbuilt checkout (e.g. `uv run` here in tests), so fall
    back to "unknown" rather than failing.
    """
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"ebay-sold-comps {pkg_version} (git {GIT_SHA}, {GIT_DATE})"

# ─── Configuration ────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".cache" / "ebay-sold-comps"
DEFAULT_CACHE_TTL_SEC = 7 * 24 * 3600  # 7 days
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_TIMEOUT_SEC = 30
DEFAULT_MAX_WORKERS = 10

# BUI-614: Tier-0 raw response capture — deliberately separate from CACHE_DIR.
# CACHE_DIR is keyed by a digest of the canonical URL (a re-query overwrites
# the prior response) and is TTL-evicted, so it silently discards history;
# this path is append-only and never read or pruned by this file, so neither
# failure mode can ever reach it. Not a substitute for a real comps ledger
# (BUI-610 designs that) — just a hedge against losing raw provider data
# before that ledger exists. Overridable for tests (see conftest.py) and for
# anyone who wants the capture off the default disk.
CAPTURE_DIR = Path(
    os.environ.get("EBAY_SOLD_COMPS_CAPTURE_DIR")
    or (Path.home() / ".local" / "share" / "ebay-sold-comps-capture")
)
CAPTURE_PATH = CAPTURE_DIR / "raw_responses.jsonl"

# BUI-628/KTD1/R16: rotate CAPTURE_PATH before it grows unbounded. Size-based,
# never age-based — the failure being prevented is a full disk, and the write
# rate is bursty (a batch writes dozens of responses in minutes, then nothing
# for days), so a clock-based rotation would either fire mid-burst for no
# reason or leave a whole burst unrotated for a long stretch. ~10 MB (roughly
# 250 responses at the measured ~40.3 KB/response average, per BUI-628) keeps
# segments a manageable size without rotating so eagerly that tiny segments
# pile up. Explicitly NOT TTL eviction — nothing is ever pruned, which is the
# entire reason this capture exists separately from the TTL-evicted CACHE_DIR.
CAPTURE_ROTATE_BYTES = int(
    os.environ.get("EBAY_SOLD_COMPS_CAPTURE_ROTATE_BYTES") or 10_000_000
)

# BUI-677: how long a retired segment must sit UNTOUCHED before compression is
# allowed to destroy its plaintext copy. Compression ends in an unlink(), and
# an unlink destroys whatever the plaintext holds at that instant — including a
# straggling append from an fd that was opened on CAPTURE_PATH just before the
# rename and has not issued its write() yet. No stat-check can close that: the
# check and the unlink cannot be made atomic against a foreign fd. BUI-628
# shipped two such checks and CI still lost 3 of 120 records under contention.
#
# So compression is DEFERRED instead of guarded: a segment is never compressed
# by the pass that retired it, only by a LATER rotation, and only once it has
# sat unwritten for this long. Both conditions are lock-free and visible
# cross-process (mtime is a filesystem fact, not process state), which matters
# because several ebay-sold-comps/comic-fmv processes share this directory.
#
# 60s is ~4 orders of magnitude more than the gap it must cover: after the
# deferral the only surviving straggler is a thread descheduled BETWEEN its
# os.open() and its os.write() — two adjacent syscalls in _capture_raw_response
# — for longer than this whole delay. Deliberately NOT env-overridable, unlike
# CAPTURE_DIR/CAPTURE_ROTATE_BYTES (which are operator choices about where the
# capture lives and how big segments get): this is a correctness parameter, and
# turning it down toward zero re-opens the data-loss window it exists to close.
CAPTURE_COMPRESS_DELAY_SEC = 60.0

# Tier thresholds (the "tiered query strategy" from the FMV skill)
THIN_RESULTS_THRESHOLD = 5     # auto-broaden (drop year) if base returns fewer
GRADE_TAGGED_THRESHOLD = 10    # add grade-targeted query if base returns fewer

# Retry policy for transient SerpApi failures (network errors, 429/5xx). The
# backoff schedule itself (2 ** attempt seconds) now lives in the shared
# ebay_fetch.retry_request() helper (BUI-333) — only the attempt count stays
# a fetch()-local knob.
FETCH_MAX_RETRIES = 3

# BUI-535: consecutive LIVE (charged) SerpApi errors — since the last live
# success, across the whole batch — before the circuit breaker trips and the
# rest of the batch is served cache-only. Calibrated against the 2026-07-24
# outage replay (~48 charged searches, every one erroring): tripping at 5
# keeps total spend on a full-outage batch in the low tens rather than the
# high forties. Some overshoot beyond exactly 5 is expected and fine — with
# DEFAULT_MAX_WORKERS concurrent workers, several requests can already be in
# flight before any of them observes the newly-tripped state.
CIRCUIT_BREAKER_THRESHOLD = 5

# ─── BUI-545: secondary provider (sold-comps.com) ────────────────────────────
#
# eBay login-walled its Sold/Completed search filters ~2026-07-23 (SerpApi
# public-roadmap#4064), killing every logged-out scraper — SerpApi's eBay sold
# engine included, with recovery contingent on eBay reverting. sold-comps.com
# is the one provider with verified post-wall sold data (smoke-tested
# 2026-07-28 against pre-wall cached SerpApi controls: 66/70 exact price
# matches, post-wall endedAt dates, structurally distinct sold/active modes —
# full trail on BUI-545). It is the DEFAULT PRIMARY provider behind
# _fetch_with_fallback() (see DEFAULT_PROVIDER_ORDER below); SerpApi remains
# in the chain as the fallback tier, taking a query only when sold-comps.com
# errors or its breaker has tripped.
SOLD_COMPS_ENDPOINT = "https://api.sold-comps.com/v1/scrape"
# Their scrape is a live retrieval with observed latencies in the tens of
# seconds — far above SerpApi's; sized to the smoke-test budget.
SOLD_COMPS_TIMEOUT_SEC = 180
# One request at their max count replaces SerpApi's ~60/page, so the BUI-523
# page-2 logic stays SerpApi-only (page>1 never routes to this provider).
SOLD_COMPS_COUNT = 240
# eBay's own sold-search window is ~90 days; matching it keeps the comp
# pool's recency profile at SerpApi parity.
SOLD_COMPS_DAYS_TO_SCRAPE = 90
# sold-comps.com rate-limits at 60 req/min. DEFAULT_MAX_WORKERS books × up to
# 4 tiers can burst past that on a big batch, and this semaphore ALONE only
# bounds how many requests are simultaneously IN FLIGHT — it does nothing to
# stop the DISPATCH rate (4 workers x ~4s responses already lands exactly on
# the 60 req/min ceiling with zero headroom, which is what let the
# 2026-08-07 batch tip into a self-reinforcing 429 storm; see the
# retrospective at docs/retrospectives/2026-08-07-gixen-outage-session.md).
# BUI-701 closed that gap with _SOLD_COMPS_RATE_LIMITER below, which is now
# the PRIMARY throttle; this semaphore remains a secondary cap on
# simultaneous connections, not the thing keeping the batch under the
# ceiling.
_SOLD_COMPS_MAX_CONCURRENCY = 4
_SOLD_COMPS_SEMAPHORE = threading.Semaphore(_SOLD_COMPS_MAX_CONCURRENCY)

# BUI-701: the token bucket the comment above used to admit was missing.
# 48 req/min (a 1.25s minimum gap between dispatches) buys ~20% headroom
# below the 60 req/min ceiling — enough to absorb ordinary jitter in
# response timing without drifting back up to the edge, while still moving
# a large batch at a reasonable pace (this replaces the manual "4 chunks x
# 10 books with 45s pauses" workaround proven during the 2026-08-07
# incident — see fmv.md's "Provider request budget" section).
_SOLD_COMPS_RATE_LIMIT_PER_MIN = 48.0


class _RateLimiter:
    """Thread-safe inter-request spacing limiter (BUI-701).

    Enforces a minimum wall-clock gap between successive request
    DISPATCHES — not completions — shared globally across every calling
    thread, so N concurrent workers collectively cannot exceed
    `rate_per_min` regardless of _SOLD_COMPS_SEMAPHORE's concurrency
    ceiling (concurrency bounds how many requests can be in flight at
    once; this bounds how fast new ones may be sent, independent of how
    long each one takes to complete).

    Implemented as a "next free slot" leaky bucket: one lock protects a
    single float (`_next_slot`), and each acquire() reserves its own slot
    under the lock, then sleeps OUTSIDE the lock for however long remains
    until that slot arrives. Sleeping outside the lock is load-bearing, not
    an optimization: a thread that must wait must not block every other
    thread's ability to compute its OWN slot and start waiting
    concurrently — only the microseconds-scale bookkeeping (read the
    current slot, bump it, release) happens under the lock; the
    potentially seconds-long wait never does.

    This is also why fetch_sold_comps() calls acquire() BEFORE acquiring
    _SOLD_COMPS_SEMAPHORE, not after, and why the semaphore itself is
    scoped to only the HTTP call (see _paced_get() there) rather than
    wrapping the whole retry_request() loop: acquiring the semaphore first
    would have a paced, sleeping thread sit on one of the four concurrency
    slots for however long it waits — including a 429's much longer BUI-701
    backoff — starving the other three workers of a slot they could
    otherwise be using right now.
    """

    def __init__(self, rate_per_min: float):
        self._interval = 60.0 / rate_per_min
        self._lock = threading.Lock()
        self._next_slot = 0.0  # monotonic time; 0.0 is always in the past

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + self._interval
        wait = start - time.monotonic()
        if wait > 0:
            time.sleep(wait)


# Module-level singleton, deliberately — like _SOLD_COMPS_SEMAPHORE, pacing
# must be shared across every caller in this process (run_batch()'s whole
# ThreadPoolExecutor and anything else on this interpreter), not scoped
# per-batch. The scope IS the process: a concurrent console-script
# invocation is a separate process with its own module copy and its own
# limiter, so two simultaneous runs can jointly exceed the 60 req/min
# ceiling. Known and accepted (BUI-706, Won't Do): single-human workflow,
# and the 48/60 pacing leaves headroom for the ordinary case. Unlike
# _CircuitBreaker, there is no per-batch "start clean" need: the rate
# limiter has no notion of tripped/untripped state to reset, only a
# next-slot clock that keeps ticking.
_SOLD_COMPS_RATE_LIMITER = _RateLimiter(_SOLD_COMPS_RATE_LIMIT_PER_MIN)

PROVIDER_SERPAPI = "serpapi"
PROVIDER_SOLD_COMPS = "sold-comps.com"
# sold-comps.com FIRST: with SerpApi's sold engine dead indefinitely behind
# the login wall, a SerpApi-primary order would pay ~CIRCUIT_BREAKER_THRESHOLD
# charged SerpApi errors at 30s+ apiece per batch before its breaker tripped.
# SerpApi stays in the chain as the fallback tier, so failover remains
# signal-driven in both directions (the BUI-545 AC); if eBay ever reverts the
# wall, set EBAY_SOLD_COMPS_PROVIDERS=serpapi,sold-comps.com to restore
# SerpApi primacy. (A no-key run still degrades to SerpApi-only.)
DEFAULT_PROVIDER_ORDER = (PROVIDER_SOLD_COMPS, PROVIDER_SERPAPI)
# Comma-ordered provider override — reorder ("serpapi,sold-comps.com") or
# restrict to one ("sold-comps.com").
PROVIDERS_ENV_VAR = "EBAY_SOLD_COMPS_PROVIDERS"


# ─── SERPAPI_KEY loader ──────────────────────────────────────────────────────

def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE per line, no quoting). Comments + blanks ok."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_serpapi_key() -> str:
    """Resolve SERPAPI_KEY from env, then apps/ebay/.env."""
    key = os.environ.get("SERPAPI_KEY")
    if key:
        return key
    app_root = Path(__file__).parent.parent
    for env_path in (app_root / ".env", app_root / ".env.local"):
        env = _load_dotenv(env_path)
        if env.get("SERPAPI_KEY"):
            return env["SERPAPI_KEY"]
    print(
        "Error: SERPAPI_KEY not found.\n"
        f"Set the env var or put it in {app_root}/.env",
        file=sys.stderr,
    )
    sys.exit(2)


def load_sold_comps_key() -> str | None:
    """Resolve SOLD_COMPS_KEY from env, then apps/ebay/.env — or None.

    Unlike load_serpapi_key() this never exits: the secondary provider is
    optional (BUI-545). Absent key → no failover tier, and the pipeline
    behaves exactly as the SerpApi-only one did (a SerpApi outage fetch-errs
    instead of failing over).
    """
    key = os.environ.get("SOLD_COMPS_KEY")
    if key:
        return key
    app_root = Path(__file__).parent.parent
    for env_path in (app_root / ".env", app_root / ".env.local"):
        env = _load_dotenv(env_path)
        if env.get("SOLD_COMPS_KEY"):
            return env["SOLD_COMPS_KEY"]
    return None


def _provider_order() -> tuple[str, ...]:
    """Resolve the provider order from PROVIDERS_ENV_VAR (default:
    sold-comps.com primary, SerpApi fallback — see DEFAULT_PROVIDER_ORDER).
    Unknown names fail loudly — a typo that silently dropped a provider
    would be an invisible config bug."""
    raw = os.environ.get(PROVIDERS_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_PROVIDER_ORDER
    order = tuple(p.strip() for p in raw.split(",") if p.strip())
    unknown = [p for p in order if p not in DEFAULT_PROVIDER_ORDER]
    if unknown or not order:
        raise ValueError(
            f"{PROVIDERS_ENV_VAR} names unknown provider(s) {unknown!r} — "
            f"valid: {', '.join(DEFAULT_PROVIDER_ORDER)}"
        )
    return order


# ─── Cache layer ──────────────────────────────────────────────────────────────

def _cache_path(canonical_url: str) -> Path:
    digest = hashlib.sha256(canonical_url.encode()).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _cache_get(path: Path, ttl_sec: int) -> tuple[dict, float] | tuple[None, None]:
    """Returns (data, mtime) on a hit, (None, None) on a miss.

    BUI-657/KTD7: `mtime` is the cache entry's file mtime — i.e. when the
    response was originally fetched, not when this lookup ran — so a cache
    hit can be stamped with the response's real fetch time rather than now.
    """
    if not path.exists():
        return None, None
    mtime = path.stat().st_mtime
    age = time.time() - mtime
    if age > ttl_sec:
        return None, None
    try:
        return json.loads(path.read_text()), mtime
    except Exception:  # noqa: BLE001  # cache read — corrupt/partial file, return None
        return None, None


def _cache_put(path: Path, data: dict) -> None:
    """Write *data* to the SerpApi response cache (BUI-333: routed through the
    shared ebay_fetch.atomic_write_json() rather than a hand-rolled
    tmp→rename copy)."""
    atomic_write_json(path, data)


# ─── BUI-614: Tier-0 raw response capture ─────────────────────────────────────

def _compress_rotated_segment(rotated_path: Path) -> None:
    """Gzip *rotated_path* in place and remove the plaintext copy.

    Purely a disk-space optimization on a segment that some EARLIER call to
    `_rotate_capture_if_needed` already `os.rename`d out of the way — the
    rename alone is what makes a rotation "count" (new appends go to a fresh
    CAPTURE_PATH from that point on), so this step's failure is independent
    of and never allowed to affect that fact.

    Never called on the segment the current pass just retired — see
    `_sweep_compressible_segments`, which is the only caller and which
    enforces both halves of the BUI-677 deferral (never this pass's own
    segment; never one written to within CAPTURE_COMPRESS_DELAY_SEC). By the
    time a segment reaches this function it has been quiescent for at least
    that long, so the "straggler holding a pre-rename fd" that BUI-628 tried
    (and failed) to guard against with stat-checks can no longer exist: it
    would have had to sit descheduled between its own os.open() and
    os.write() for the whole delay.

    The size checks below are kept as belt-and-braces behind that deferral,
    not as the primary guard — BUI-628's mistake was believing a stat could
    be atomic against a foreign fd, and CI disproved it. If a write does land
    mid-compress, the plaintext segment is left exactly as it is:
    uncompressed but intact, authoritative, and picked up again by a later
    rotation's sweep. Every branch here resolves toward "keep the plaintext",
    never toward "trust the .gz".

    Multi-process note (BUI-677): because compression is now deferred rather
    than done by the process that rotated, two processes' sweeps can pick the
    same eligible segment at once. The `.tmp` name therefore carries a
    per-attempt pid+token so concurrent attempts cannot clobber each other's
    partial gzip, and every step that touches the plaintext tolerates it
    having already been compressed and removed by the peer. Both sweeps read
    the same quiescent bytes, so whichever `os.replace` lands last publishes
    a complete, content-equal `.gz`.

    A failure partway through the gzip write (e.g. disk full, mirroring
    ebay_fetch.atomic_write_json's BUI-333 fix) can leave an orphaned `.tmp`
    file behind; it is best-effort unlinked in the except block below, same
    as that precedent, so a repeat failure doesn't litter the capture dir.
    """
    gz_path = rotated_path.with_name(rotated_path.name + ".gz")
    tmp_path = gz_path.with_name(f"{gz_path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        try:
            before = rotated_path.stat().st_size
            raw = rotated_path.read_bytes()
        except FileNotFoundError:
            return  # a peer sweep already compressed and removed it
        if len(raw) != before or rotated_path.stat().st_size != before:
            return  # a straggling append landed mid-read; leave it plaintext
        with gzip.open(tmp_path, "wb") as f:
            f.write(raw)
        try:
            if rotated_path.stat().st_size != before:
                _unlink_quietly(tmp_path)
                return  # grew while compressing — leave the plaintext authoritative
        except FileNotFoundError:
            # A peer sweep finished the whole job while we compressed. Its .gz
            # is already published; do NOT os.replace ours over it — ours was
            # read earlier and could be the shorter of the two.
            _unlink_quietly(tmp_path)
            return
        os.replace(tmp_path, gz_path)
        try:
            if rotated_path.stat().st_size != before:
                # A straggler landed between the read above and here, so the
                # .gz we just published is missing its line. LEAVE BOTH: the
                # plaintext is authoritative and complete, and a reader that
                # finds a segment in both forms reads the plaintext and skips
                # the .gz (the BUI-661 backfill's `capture_segments`). The
                # next sweep re-compresses this segment and os.replace()s a
                # correct .gz over the stale one, so this self-heals.
                #
                # Deliberately NOT unlinking the .gz here: this process cannot
                # tell its own .gz from one a peer sweep published a moment
                # ago, and deleting a peer's COMPLETE .gz just before that
                # peer unlinks the plaintext would lose the whole segment
                # rather than one record. Leaving a redundant .gz costs disk;
                # removing the wrong one costs data.
                return
            rotated_path.unlink()
        except FileNotFoundError:
            return  # peer removed the plaintext; our .gz holds the same bytes
    except Exception as exc:  # noqa: BLE001  # never let compression touch the append path
        _unlink_quietly(tmp_path)  # best-effort — never mask the original failure below
        print(
            f"BUI-628: capture rotation compress failed (non-fatal, "
            f"segment kept uncompressed): {exc}", file=sys.stderr,
        )


def _unlink_quietly(path: Path) -> None:
    """Best-effort unlink — used only for cleanup paths where the file's
    absence is as good as its removal, so an error here must never surface."""
    try:
        path.unlink()
    except OSError:
        pass


def _sweep_compressible_segments(just_rotated: Path) -> None:
    """Gzip every retired plaintext segment that is safe to compress.

    BUI-677: this is the deferral. *just_rotated* — the segment this very
    pass retired — is skipped unconditionally, because that is the one
    segment a straggling append can still be about to land in (its fd was
    resolved before the rename). Every other retired plaintext segment is
    compressed only once its mtime shows it has taken no write for
    CAPTURE_COMPRESS_DELAY_SEC.

    mtime, not the rotation timestamp in the filename, is the right clock:
    it is bumped by the straggler's own write, so an actively-written segment
    keeps postponing its own compression, and it is equally visible to a
    sweep running in a different process. A segment whose mtime is somehow in
    the future is skipped, which is the safe direction.

    Called only from inside `_rotate_capture_if_needed`'s rotation branch, so
    the directory glob costs nothing on the hot append path — it runs once
    per CAPTURE_ROTATE_BYTES written, not once per response. The consequence
    is explicit and accepted: if this process never rotates again, the last
    retired segment stays plaintext forever. An uncompressed segment is a
    disk-space cost; a compressed-away record is unrecoverable. Readers glob
    both extensions (see the BUI-661 backfill's `capture_segments`), so a
    segment left plaintext is fully readable, never lost.

    A failed compress is not fatal and not final: `_compress_rotated_segment`
    swallows and logs its own errors, and the segment stays eligible for the
    next rotation's sweep.

    RESIDUAL WINDOW, stated honestly rather than claimed away (the mistake
    BUI-677 was): this is a time bound, not a proof. One interleaving still
    loses a record — an appender that resolves CAPTURE_PATH with os.open(),
    is then descheduled for longer than CAPTURE_COMPRESS_DELAY_SEC *between
    that open and its os.write*, and only then writes into a segment a sweep
    has meanwhile compressed and unlinked. Those are two adjacent syscalls
    with nothing but a `try:` between them, so reaching 60s takes something
    like a SIGSTOP, a suspended VM or a machine sleeping mid-append. Closing
    it completely needs a lock held across rotate+compress AND around every
    append's open — rejected: it puts a lock on the hot fetch path, for a
    hedge, to buy the difference between "practically impossible" and
    "impossible".
    """
    try:
        candidates = sorted(CAPTURE_DIR.glob("raw_responses.*.jsonl"))
    except OSError as exc:
        print(
            f"BUI-628: capture segment sweep failed (non-fatal, segments kept "
            f"uncompressed): {exc}", file=sys.stderr,
        )
        return
    now = time.time()
    for path in candidates:
        if path == just_rotated or path == CAPTURE_PATH:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # vanished under us (a peer sweep) — nothing to do
        if now - mtime < CAPTURE_COMPRESS_DELAY_SEC:
            continue  # too fresh: a straggling append could still be in flight
        _compress_rotated_segment(path)


def _rotate_capture_if_needed() -> None:
    """Rotate CAPTURE_PATH to a timestamped, gzip-compressed segment once it
    crosses CAPTURE_ROTATE_BYTES (BUI-628/KTD1/R16).

    Called from _capture_raw_response() before every append, inside its own
    try/except there — ANY exception raised here (an unwritable directory, a
    concurrent rotation racing this one and winning, ...) is caught by the
    caller, logged, and swallowed: it degrades to "CAPTURE_PATH is still
    whatever it was" and the caller's own append proceeds against that path
    unchanged. Rotation itself is never allowed to lose the append that
    triggered it — at worst the file stays over-threshold for one more
    write and gets picked up by the next call.

    The rotated name carries a UTC timestamp plus a short random token, not
    the timestamp alone: two processes racing this same check within the
    same rotation cycle could otherwise both mint the identical timestamped
    name, and os.rename() onto an EXISTING destination silently overwrites
    it — which would destroy whichever segment lost that race. The random
    token makes that collision practically impossible without needing an
    existence-check-and-retry loop.

    BUI-677: the segment this call retires is deliberately NOT compressed
    here. Compression destroys the plaintext, and a straggling append can
    still land in a segment that was CAPTURE_PATH a moment ago. The sweep
    below compresses only OLDER, quiescent segments; this one becomes
    eligible for a later rotation's sweep. See _sweep_compressible_segments.
    """
    try:
        size = CAPTURE_PATH.stat().st_size
    except FileNotFoundError:
        return  # nothing captured yet — nothing to rotate
    if size < CAPTURE_ROTATE_BYTES:
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = uuid.uuid4().hex[:8]
    rotated = CAPTURE_DIR / f"raw_responses.{ts}-{token}.jsonl"
    os.rename(CAPTURE_PATH, rotated)  # atomic — the new CAPTURE_PATH starts empty
    _sweep_compressible_segments(just_rotated=rotated)


def _capture_raw_response(
    provider: str, query: str, canonical_url: str, data: dict,
    validation: str = "ok",
) -> None:
    """Append the raw provider response, as received, to CAPTURE_PATH.

    Hedge, not a dependency: this runs right alongside _cache_put() for a
    validated response, and — since BUI-628/KTD12 — also for a response that
    FAILED our own shape validation (a dropped LH_Sold=1, a sold-comps.com
    shape violation): callers now capture the raw body before deciding
    whether to raise, passing `validation="ok"` or the validation error
    string. `data` is written verbatim either way, with no
    reshaping/parsing/field-dropping — that judgment belongs to the real
    ledger (BUI-610), not here. `validation` is keyword-only-by-convention
    with a default so the 4-positional-arg call shape stays exactly what it
    was for BUI-614 (another agent reads this function's output format).
    Any failure in this whole function (disk full, permission error, an
    unwritable path, a rotation failure) is logged to stderr and swallowed
    — it must never fail the fetch it's shadowing (BUI-614 AC).

    JSONL append chosen over a SQLite table: no schema/migration to design
    (out of scope per the ticket), and a single os.open(O_APPEND) + os.write()
    call below is a single write(2) syscall, which POSIX guarantees is
    atomic against other appenders (other threads in this process, and other
    concurrent `ebay-sold-comps`/`comic-fmv` processes) — so no separate lock
    file is needed, and a crash mid-write can only ever tear the one
    in-flight line, never a previously-appended one. BUI-628's rotation
    (_rotate_capture_if_needed, called first, below) preserves this: it only
    ever moves the *whole file* out from under CAPTURE_PATH via one atomic
    os.rename(), which either happens entirely before this call's open() or
    entirely after it — there is no window where a partial rotation could
    make this call's single write(2) non-atomic.

    A straggling append — one whose os.open() resolved CAPTURE_PATH just
    before a concurrent rename, so its write(2) lands in the just-retired
    segment rather than the fresh one — is tolerated, but the reason is
    NOT the rename's atomicity alone. This docstring used to claim that the
    straggler was "never a lost one" because it landed "in a different,
    still fully-readable segment"; CI disproved it (BUI-677: 3 of 120
    records lost). The rename argument above is sound and stops where the
    rename does — it says nothing about what happens to that segment
    afterwards, and BUI-628's compression then UNLINKED the plaintext the
    straggler had just written into. What actually makes the straggler safe
    is BUI-677's deferral: no segment is ever compressed by the pass that
    retired it, nor until it has taken no write for
    CAPTURE_COMPRESS_DELAY_SEC — so by the time the plaintext is destroyed,
    no fd that could still write to it can plausibly exist. See
    _sweep_compressible_segments for the residual window that leaves.
    """
    try:
        _rotate_capture_if_needed()
    except Exception as exc:  # noqa: BLE001  # rotation is itself a hedge — never block the append
        print(
            f"BUI-628: capture rotation failed (non-fatal, appending to "
            f"current file): {exc}", file=sys.stderr,
        )

    try:
        record = {
            "timestamp": time.time(),
            "provider": provider,
            "query": query,
            "canonical_url": canonical_url,
            "validation": validation,
            "response": data,
        }
        line = (json.dumps(record) + "\n").encode("utf-8")
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(CAPTURE_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001  # hedge — never fail the fetch (BUI-614)
        print(f"BUI-614: raw response capture failed (non-fatal): {exc}", file=sys.stderr)


# ─── Query construction ──────────────────────────────────────────────────────

_MARVEL_QUALIFIER = "marvel comics"

# BUI-321: known DC/Marvel imprints → their PARENT publisher's gate. Without
# this table these imprints don't match \bmarvel\b/\bdc\b, so they fall through
# to the indie raw-passthrough branch and append the imprint name (e.g.
# "Vertigo", "Epic") as a query keyword — recall noise, since eBay comic
# listings title the parent publisher, not the imprint. Keys are the
# punctuation-stripped, lowercased, whitespace-collapsed publisher string (see
# _normalize_publisher_key). Values are the parent gate: "marvel" → the Marvel
# qualifier, "dc" → no qualifier (Marvel-only gate, BUI-315).
_IMPRINT_PARENT_GATE = {
    # ── Marvel imprints ──
    "epic": "marvel",
    "epic comics": "marvel",
    "icon": "marvel",
    "icon comics": "marvel",
    "max": "marvel",
    "max comics": "marvel",
    "marvel knights": "marvel",
    "star comics": "marvel",
    "timely": "marvel",
    "timely comics": "marvel",
    # NOTE: Malibu is deliberately NOT mapped — it published independently
    # (1986–1994) before Marvel acquired it, so a year-less "marvel comics"
    # qualifier would over-narrow pre-acquisition titles (Ultraverse, Men in
    # Black). It falls to indie passthrough, appending "Malibu" — correct for
    # both eras, since those listings say "Malibu", not "Marvel". (BUI-321)
    # ── DC imprints ──
    "vertigo": "dc",
    "dc vertigo": "dc",
    "wildstorm": "dc",
    "black label": "dc",
    "dc black label": "dc",
    "milestone": "dc",
    "milestone media": "dc",
    "milestone comics": "dc",
    "paradox press": "dc",
    "minx": "dc",
    "helix": "dc",
    "homage": "dc",
    "homage comics": "dc",
    "zuda": "dc",
    "zuda comics": "dc",
}


def _normalize_publisher_key(publisher: str) -> str:
    r"""Lowercase, drop periods, collapse whitespace — a match key.

    Dropping periods is what lets "D.C." reach the \bdc\b gate (BUI-321): the
    raw string "D.C." has no "dc" whole-word token, so it previously missed the
    gate and got appended as a raw "D.C." keyword. Periods are removed (not
    spaced) so "D.C." collapses to "dc", not "d c".
    """
    key = publisher.replace(".", "")
    return re.sub(r"\s+", " ", key).strip().lower()


def _publisher_qualifier(publisher: str | None) -> str | None:
    """Normalize a publisher into the query qualifier keyword to append.

    BUI-304 (issue 2): for Marvel we emit the canonical "marvel comics" — a
    cheap disambiguator that keeps the *year-less* base query (per /comic:buy's
    convention of omitting year to dodge the BUI-129 collection-check
    false-negative) from colliding with modern media that reuses the issue
    number: e.g. "X-Men 97" vs the 2024 "X-Men '97" show's merchandise.

    BUI-315 — Marvel ONLY: a live SerpApi spot-check (BUI-304) showed the Marvel
    qualifier is neutral-to-positive (ASM 300 46→50, X-Men 97 44→49) but the DC
    "dc comics" two-token qualifier MATERIALLY narrows recall (Batman 232 34→12,
    Detective 400 38→21). So DC recognized publishers get NO qualifier (return
    None) — the base query passes through untouched rather than regressing. Any
    "DC Comics" raw passthrough would reintroduce the same two-token narrowing,
    so DC must short-circuit to None, not fall to the indie branch.

    BUI-321: known DC/Marvel imprints (Vertigo, Wildstorm, Epic, …) map to their
    parent's gate via _IMPRINT_PARENT_GATE instead of falling to indie
    passthrough, and punctuation is tolerated so "D.C." is gated (not appended).

    Indie publishers pass through unchanged — the caller already supplies the
    noise-filtering name ("image comics", "dark horse"), which is the primary
    indie noise filter (BUI-161). Returns None for an absent/blank publisher so
    the base query is untouched.
    """
    if not publisher or not publisher.strip():
        return None
    p = publisher.strip()
    key = _normalize_publisher_key(p)
    # BUI-321: resolve a known imprint to its parent gate first; else match the
    # parent name directly on the punctuation-normalized key ("D.C." → "dc").
    gate = _IMPRINT_PARENT_GATE.get(key)
    if gate == "marvel" or re.search(r"\bmarvel\b", key):
        return _MARVEL_QUALIFIER
    if gate == "dc" or re.search(r"\bdc\b", key):
        return None  # BUI-315: DC qualifier regresses recall — no qualifier
    return p


_LEADING_ARTICLE_RE = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)


def _strip_leading_article(title: str) -> str:
    """Strip a leading article ("The"/"A"/"An") from a series title.

    BUI-346 defense-in-depth: `build_query` normalizes its own `title` input
    independent of whatever normalization (or lack of it) happened upstream in
    the buy→FMV handoff (apps/fmv/src/fmv_runner.py does the same strip at the
    working-list boundary). Duplicated rather than shared across apps/ebay and
    apps/fmv per this repo's existing package boundary — comic-fmv shells out
    to the ebay-sold-comps console script rather than importing it (see
    CLAUDE.md's "FMV pipeline shells out across package boundaries").
    """
    return _LEADING_ARTICLE_RE.sub('', title or '').strip()


def _strip_embedded_issue(title: str, issue: str) -> str:
    """Strip an embedded ``#<issue>`` (or a bare trailing issue token) from
    *title* when it duplicates the separate `issue` field.

    BUI-346: without this, a title like "The Amazing Spider-Man #50" combined
    with issue="50" makes the `f'"{title} {issue}"'` phrase double up into
    `"The Amazing Spider-Man #50 50"`, which returns 0 results on every tier
    (real incident: ASM #50, 2026-07-13 buy run). The `(?<!\\d)` guard on the
    trailing-token strip prevents chewing into an unrelated longer number
    (e.g. issue="99" must not touch the "2099" in "X-Men 2099").
    """
    issue = str(issue).strip() if issue else ""
    if not title or not issue:
        return title
    cleaned = re.sub(rf'#\s*{re.escape(issue)}\b', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(rf'(?<!\d){re.escape(issue)}\s*$', '', cleaned.strip())
    return re.sub(r'\s+', ' ', cleaned).strip()


# BUI-347: rebootable mastheads — long-running Marvel/DC titles whose numbering
# (or a same-numbered modern relaunch) collides with a vintage issue's own
# number. List mirrors the one used by locg-cli's analogous collection-check
# ambiguity (packages/locg-cli/src/locg/check_batch.py's Pattern D3) so the
# two "which titles are rebootable" judgment calls don't drift apart — kept
# in sync by test_check_batch.py::test_rebootable_masthead_list_matches_sold_comps
# (BUI-577). Update both lists together.
_REBOOTABLE_MASTHEADS = (
    "fantastic four", "amazing spider-man", "spider-man", "spiderman",
    "uncanny x-men", "x-men", "avengers", "thor", "iron man",
    "incredible hulk", "hulk", "captain america", "batman", "superman",
    "wonder woman",
)
# BUI-351: plain `\b` treats a hyphen as a non-word char, so `\bhulk\b` matches
# INSIDE "She-Hulk" (the boundary lands on the "-"). Anchor on a full
# title-token match instead: forbid the masthead from being immediately
# preceded or followed by a hyphen (or any other word char), so it can't be a
# substring of a different hyphenated title. A masthead's OWN internal hyphen
# (e.g. "spider-man") is untouched — re.escape keeps it literal; only the
# match's outer edges get the tightened boundary.
_REBOOTABLE_MASTHEAD_RES = [
    re.compile(rf'(?<![-\w]){re.escape(m)}(?![-\w])', re.IGNORECASE)
    for m in _REBOOTABLE_MASTHEADS
]

# Pre-2000 gate for "vintage" (BUI-347's own example threshold). Deliberately
# simple/conservative — this is a hard gate, not a fuzzy score, so a modern
# book's query is byte-for-byte unaffected.
_VINTAGE_YEAR_CUTOFF = 2000

# Conservative exclusion lexicon — every token here is a modern
# printing/cover-variant convention that could not appear in a genuine 1960s/
# 70s raw comic listing (variant covers, foil covers, "virgin"/no-logo covers,
# and the "Timeless"/"Homage" modern cover programs are all post-1990s
# inventions; "reprint"/"facsimile" describe a later, non-original printing —
# exactly what a vintage-key comp query must exclude). Money-safety
# (BUI-347): do NOT add anything broader than this without re-validating
# against a real vintage sold-comp pool — see
# test_vintage_comp_pool_survives_exclusion_terms.
_VINTAGE_EXCLUSION_TERMS = (
    "-variant", "-foil", "-virgin", "-reprint", "-facsimile", "-homage",
    "-timeless",
)


def _is_rebootable_masthead(title: str) -> bool:
    """True if *title* names a long-running masthead with a modern relaunch
    that reuses low issue numbers (BUI-347)."""
    return any(p.search(title or '') for p in _REBOOTABLE_MASTHEAD_RES)


# BUI-581: masthead RENAME pairs — two names one long-running series carried at
# different points in its own run, so the SAME physical book is listed under
# either. X-Men Vol.1 is the documented case: the low issue numbers were
# published as *X-Men* and the *Uncanny* masthead only appears from the rename
# onward, so a query built from the modern name finds almost nothing on a
# vintage issue and the near-empty pool is then priced as if the book were
# illiquid. Measured on the live DB, the same five books 11 minutes apart:
# #69 n=6→$15 as "X-Men" vs n=0 as "Uncanny X-Men"; #75 4 vs 0; #85 4 vs 1;
# #93 4 vs 1; #97 30 vs 3.
#
# DELIBERATELY NOT a rename-ISSUE-NUMBER table. Which issue the masthead
# actually changed on is not something this module can assert (BUI-581 flagged
# its own "commonly cited around #114" as unverified), and a wrong cutoff would
# silently route real books to the wrong name — the very failure this fixes.
# So the pool decides instead of a hardcoded number: `fetch_book_comps` probes
# the counterpart masthead and keeps whichever pool is DEEPER (see its
# alt-masthead tier). A pair that doesn't apply to a given issue simply loses
# that comparison and costs one query.
#
# Kept SEPARATE from `_REBOOTABLE_MASTHEADS` above on purpose — that list
# answers "could this title's issue number collide with a modern relaunch's?"
# (an EXCLUSION signal, mirrored in locg-cli and pinned by
# test_rebootable_masthead_list_matches_sold_comps); this one answers "what else
# was this exact run called?" (a SUBSTITUTION signal). Different judgment,
# different consumers — do not merge or sync them.
#
# Only the pair BUI-581 measured is listed. Other renamed runs (Journey into
# Mystery → Thor, Tales of Suspense → Iron Man, …) are plausible but unverified
# here, and each unverified pair costs a real provider query on every thin
# vintage pool for that masthead — add one only with pool evidence behind it.
_MASTHEAD_RENAME_PAIRS = (
    # Ordered longest-first: "uncanny x-men" must be tested before the bare
    # "x-men" it contains, or the specific name would rewrite to itself.
    ("uncanny x-men", "X-Men"),
    ("x-men", "Uncanny X-Men"),
)
# Anchored at the START of the (article-stripped) title, with the BUI-351
# boundary on the trailing edge. Anchoring keeps the substitution off titles
# that merely CONTAIN the masthead ("Giant-Size X-Men", "Wolverine and the
# X-Men") where swapping the name produces a title no listing ever carried.
_MASTHEAD_RENAME_RES = tuple(
    (re.compile(rf'^{re.escape(name)}(?![-\w])', re.IGNORECASE), replacement)
    for name, replacement in _MASTHEAD_RENAME_PAIRS
)


def _alias_masthead_title(title: str) -> str | None:
    """The same run's OTHER masthead for *title*, or None when it has no known
    counterpart (BUI-581).

    Purely a name substitution: it makes NO claim about which issues carried
    which masthead. The caller decides whether the alias is the right name for
    this book by comparing the two comp pools — see `_MASTHEAD_RENAME_PAIRS`.
    """
    base = _strip_leading_article(title or '')
    for pattern, replacement in _MASTHEAD_RENAME_RES:
        alias, hits = pattern.subn(replacement, base, count=1)
        if not hits:
            continue
        alias = re.sub(r'\s+', ' ', alias).strip()
        # A pair that rewrites a title to itself would burn a query on a
        # byte-identical search.
        return alias if alias.casefold() != base.casefold() else None
    return None


def _coerce_year(value: object) -> int | None:
    """Coerce a cover year to `int`, or None when there is no usable year.

    BUI-565: `year` is a documented `--batch` field and `/comic:identify`
    emits it as a **string** (`"1976"`), but every year test in this module
    compares against the int `_VINTAGE_YEAR_CUTOFF`. An uncoerced string
    therefore raised `TypeError: '<' not supported between instances of 'str'
    and 'int'` inside `build_query`'s vintage gate — on the very first tier,
    before any query ran. `fetch_book_comps`' broad handler caught it and
    returned an EMPTY `queries_used`, which comic-fmv's `_is_fetch_error`
    reads as a genuine no-comps book. Result: a silent `n=0` with no
    `flag_reason` on a money path. Coerce once, here at the boundary, so a
    string year is first-class input instead of a crash.

    An empty/whitespace string means "no year" (that already worked — `""` is
    falsy, so it short-circuited the gate rather than crashing) and is
    preserved as None. Anything else that can't be read as a year **raises**
    rather than being silently dropped: a year-less query still returns comps,
    so dropping it would swap a loud failure for a quietly *different* search
    that the operator never learns about. The raise surfaces as this book's
    `error` (see `fetch_book_comps`), which the caller routes to needs-manual.
    """
    if value is None:
        return None
    # bool is an int subclass — `True` would otherwise sail through as year 1
    # and put the literal "True" in the query text.
    if isinstance(value, bool):
        raise ValueError(f"year must be a number or numeric string, got {value!r}")
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):  # NaN / inf
            raise ValueError(f"unparseable year {value!r}") from None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return int(float(s))
        except ValueError:
            raise ValueError(f"unparseable year {value!r}") from None
    raise ValueError(f"year must be a number or numeric string, got {value!r}")


def build_query(title: str, issue: str, year: int | str | None = None,
                publisher: str | None = None, variant: str | None = None,
                grade_label: str | None = None,
                exclude_graded: bool = True,
                vintage_year: int | str | None = None) -> str:
    """Build the _nkw search string. Returns the raw (unencoded) keyword string.

    `vintage_year` (BUI-350): the book's real cover year, used ONLY to gate the
    BUI-347 vintage-masthead exclusion terms — independent of whether `year`
    itself is embedded in the query text. `fetch_book_comps`'s tier-2 "broaden"
    query drops `year` (to widen recall) but must not thereby drop the vintage
    hardening: a rebootable-masthead vintage key's broadened (year-less) query
    could otherwise blend in modern slabbed variants. Callers that broaden pass
    the original year here while passing `year=None` for the query text.
    Defaults to `year` itself, so every existing caller that doesn't pass it
    keeps byte-for-byte pre-BUI-350 behavior.

    BUI-565: `year`/`vintage_year` accept a numeric string (`"1976"`) as well
    as an int — see `_coerce_year` for why that is the natural input and what
    used to happen to it. An int caller's query is byte-for-byte unchanged.
    """
    # BUI-565: coerce BEFORE anything compares against `_VINTAGE_YEAR_CUTOFF`.
    year = _coerce_year(year)
    vintage_year = _coerce_year(vintage_year)
    # BUI-346: normalize the title before it's ever quoted — strip a leading
    # article, then an embedded/trailing issue number that would otherwise
    # double up with the separate `issue` field below. Guarded on a truthy
    # title so a falsy/absent one (not a real, expected input, but `title` is
    # a plain `str` param with no caller ever passing None in practice) keeps
    # its pre-BUI-346 byte-for-byte behavior rather than silently becoming an
    # empty string.
    if title:
        title = _strip_embedded_issue(_strip_leading_article(title), issue)
    parts = [f'"{title} {issue}"']
    if year:
        parts.append(str(year))
    # BUI-304 (issue 1): append the distribution variant (e.g. "Newsstand",
    # "Direct") as a query keyword, mirroring the publisher mechanism below.
    # Previously `variant` was DB-only (distinct comic_id per BUI-28) and never
    # reached the search — so a plain "X-Men 123" blended newsstand + direct
    # copies, and after grade-parsing losses too few remained attributable to
    # either sub-market. Guard for empty/None so the base query is unchanged
    # (byte-for-byte) when variant is absent.
    variant = variant.strip() if variant else ""
    if variant:
        parts.append(variant)
    # BUI-304 (issue 2): the publisher qualifier — indie passes through, Marvel
    # normalizes to "marvel comics"; DC gets none (BUI-315). See
    # _publisher_qualifier.
    qualifier = _publisher_qualifier(publisher)
    if qualifier:
        parts.append(qualifier)
    if grade_label:
        parts.append(grade_label)
    # BUI-347: harden a vintage key's comp query against its own modern
    # relaunch. Gated HARD on old-year AND a rebootable masthead — a modern
    # book (recent year, or no year at all) or a non-rebootable title is
    # completely untouched by this branch, so its query stays byte-for-byte
    # identical to pre-BUI-347 output. BUI-350: gate on `vintage_year`, not
    # `year` — see the `vintage_year` docstring above for why.
    gate_year = year if vintage_year is None else vintage_year
    if gate_year and gate_year < _VINTAGE_YEAR_CUTOFF and _is_rebootable_masthead(title):
        parts.extend(_VINTAGE_EXCLUSION_TERMS)
    if exclude_graded:
        parts.extend(["-cgc", "-cbcs", "-graded", "-slab"])
    return " ".join(parts)


def canonical_serpapi_url(nkw: str, *, page: int = 1) -> str:
    """Build the SerpApi URL with deterministic param order (for cache key).

    Excludes api_key from the canonical form so we don't tie cache to a
    specific user's key. The actual request URL adds api_key separately.

    BUI-523: `page` selects SerpApi's eBay-engine page-number param (`_pgn`).
    It's omitted entirely for `page=1` — the default, and the only page any
    pre-BUI-523 caller ever requested — so the canonical URL (and therefore
    the cache key) is byte-for-byte unchanged for the common case; the
    existing 7-day cache isn't invalidated by this change. Page 2+ appends
    `_pgn` to the params, giving each page its own cache key so a page-2
    fetch caches independently of page 1 under the same TTL (required so a
    repeat FMV run within the TTL window doesn't re-bill SerpApi for a page
    it already paid for).
    """
    params = {
        "engine": "ebay",
        "_nkw": nkw,
        "show_only": "Sold",
    }
    if page and page > 1:
        params["_pgn"] = page
    canonical = urllib.parse.urlencode(sorted(params.items()))
    return f"{SERPAPI_ENDPOINT}?{canonical}"


def request_url(canonical: str, api_key: str) -> str:
    sep = "&" if "?" in canonical else "?"
    return f"{canonical}{sep}api_key={api_key}"


def canonical_sold_comps_url(nkw: str) -> str:
    """Build the sold-comps.com request URL with deterministic param order.

    Doubles as the cache key (BUI-545). The API key rides the Authorization
    header, never the URL, so unlike SerpApi there is no separate
    request_url() step. `sold=true` is their default but is pinned explicitly
    so the cache key documents the semantics it was fetched under (the
    generalized LH_Sold=1 lesson). `includeCompleteListing=true` is pinned the
    same way (BUI-557) — also their default, so this is no behavior change,
    but BUI-552's evaluation showed the flag silently controls whether OBO
    badge detection works at all: refetching with
    `includeCompleteListing=false` flipped OBO detection from 73 items to 0,
    while `soldPrice` stayed byte-identical across those 73 OBO items and 150
    non-OBO controls. An unpinned vendor-side default flip would be a silent
    drift risk, so pin it rather than rely on the default. The endpoint host
    makes these sha256 cache keys disjoint from every SerpApi entry — nothing
    pre-BUI-545 is invalidated.
    """
    params = {
        "count": SOLD_COMPS_COUNT,
        "daysToScrape": SOLD_COMPS_DAYS_TO_SCRAPE,
        "includeCompleteListing": "true",
        "keyword": nkw,
        "sold": "true",
    }
    canonical = urllib.parse.urlencode(sorted(params.items()))
    return f"{SOLD_COMPS_ENDPOINT}?{canonical}"


# ─── Fetch (with cache + URL verification) ────────────────────────────────────

class SerpApiError(Exception):
    pass


class BreakerTrippedError(SerpApiError):
    """BUI-535: raised by fetch() when the batch circuit breaker has already
    tripped and no cache entry covers this query. Subclasses SerpApiError so
    every existing `except (SerpApiError, requests.RequestException)` site
    (fetch_book_comps._run, in particular) keeps catching it unchanged — from
    every caller's point of view this is an ordinary fetch failure, just one
    that never actually reached SerpApi."""


class SoldCompsError(SerpApiError):
    """BUI-545: a sold-comps.com (secondary provider) failure. Subclasses
    SerpApiError for the same reason BreakerTrippedError does — every
    existing `except (SerpApiError, requests.RequestException)` site keeps
    catching it unchanged; from a caller's point of view it is an ordinary
    fetch failure that happens to come from the other provider."""


class _CircuitBreaker:
    """BUI-535: batch-scoped SerpApi outage circuit breaker.

    One instance is created per `run_batch()` call and threaded explicitly
    into every `fetch_book_comps()`/`fetch()` call for that batch (via the
    `breaker=` kwarg) — NOT a module-level singleton, so each fresh batch
    starts clean regardless of what happened in a previous run.

    "Consecutive" is defined globally across the whole batch, not per-thread
    or per-book: K live (charged) SerpApi errors with no live success in
    between, counted since the last live success (or since the batch began).
    A cache hit neither trips nor resets it — only a genuine live SerpApi
    round trip counts, whether it's the winning attempt of a `fetch()` call or
    one of that call's own superseded internal retries (see `fetch()`'s
    `record_attempt`/`on_attempt` wiring) — each one is a real SerpApi charge.

    Thread-safety: every read/mutation of the counter and `tripped` happens
    under one lock, so two threads whose live-errors land at the same instant
    can't both independently conclude "I'm the one crossing the threshold" —
    record_error() prints the one-time loud warning INSIDE the same locked
    critical section that detects the crossing, so it is structurally
    impossible for two threads to both print it.

    Once tripped, stays tripped for the rest of this batch — it does not
    un-trip if SerpApi recovers mid-batch (see record_success()). Recovery is
    a fresh process/run_batch() call (a new, untripped breaker), matching the
    "re-run later" guidance in the printed warning.
    """

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD, *,
                 total_books: int = 0, provider_name: str = "SerpApi"):
        self.threshold = threshold
        self._lock = threading.Lock()
        self._consecutive_errors = 0
        self.tripped = False
        self._total_books = total_books
        self._completed_books = 0
        # BUI-545: names the provider in the trip warning — each provider gets
        # its own breaker instance now, so the message must say which one died.
        self._provider_name = provider_name

    def should_skip_live(self) -> bool:
        with self._lock:
            return self.tripped

    def record_success(self) -> None:
        """A genuine LIVE (charged) SerpApi call succeeded — resets the
        streak. Does not clear `tripped`: once tripped, this batch stays
        cache-only for the remainder (see class docstring)."""
        with self._lock:
            self._consecutive_errors = 0

    def record_error(self) -> None:
        """A genuine LIVE (charged) SerpApi call failed. Trips the breaker
        and prints the one-time loud stderr warning the instant the
        threshold is crossed — see class docstring for why this can only
        ever fire once."""
        with self._lock:
            if self.tripped:
                return
            self._consecutive_errors += 1
            if self._consecutive_errors >= self.threshold:
                self.tripped = True
                remaining = max(self._total_books - self._completed_books, 0)
                print(
                    f"{self._provider_name} appears down — "
                    f"{self._consecutive_errors} consecutive errors, circuit "
                    f"breaker tripped (BUI-535). Skipping live "
                    f"{self._provider_name} fetches for the remaining "
                    f"~{remaining} book(s) in this batch; re-run later.",
                    file=sys.stderr,
                )

    def book_completed(self) -> None:
        """Called once per book as its future finishes — feeds the `remaining
        ~N book(s)` estimate above. Approximate by nature under concurrency
        (several books may already be in flight when the trip happens), which
        is fine for an informational warning, not a correctness signal."""
        with self._lock:
            self._completed_books += 1


def fetch(nkw: str, api_key: str, *, force: bool = False,
          ttl_sec: int = DEFAULT_CACHE_TTL_SEC, page: int = 1,
          record_attempt=None, breaker: "_CircuitBreaker | None" = None,
          ) -> tuple[dict, bool, float]:
    """Fetch a SerpApi response with caching. Returns
    (data, cache_hit, response_fetched_at).

    BUI-523: `page` (default 1) selects the SerpApi page — see
    canonical_serpapi_url for why page 1 stays byte-for-byte identical to
    pre-BUI-523 behavior and page 2+ gets its own cache entry.

    BUI-537: `record_attempt`, when given, is called as
    `record_attempt(outcome: str, detail: str)` once for every internal retry
    attempt this call supersedes with a further attempt — i.e. every SerpApi
    charge that would otherwise be invisible to the caller. It is NOT called
    for the terminal attempt (the one this function ultimately returns/raises
    for) — the caller already learns that outcome from the normal return
    value / raised exception, so calling the hook there too would double-
    count that one charge.

    BUI-535: `breaker`, when given, gates every live (charged) attempt through
    the batch-scoped circuit breaker (see `_CircuitBreaker`): every charged
    attempt (interim retry or terminal) reports success/failure to it, and a
    tripped breaker short-circuits this call to a cache-only lookup (raising
    `BreakerTrippedError` on a miss) — even under `force=True`, which
    otherwise means "bypass the cache," but does not mean "bypass the
    breaker."

    BUI-657/KTD7: `response_fetched_at` is the cache entry's mtime on a hit,
    or `time.time()` at the moment the live response was validated on a live
    fetch — never "now" for a cache hit. A re-run of a stable book must not
    restate three-month-old comps as observed today.
    """
    canonical = canonical_serpapi_url(nkw, page=page)
    path = _cache_path(canonical)

    cache_checked = False
    if not force:
        cached, cached_mtime = _cache_get(path, ttl_sec)
        cache_checked = True
        if cached is not None:
            return cached, True, cached_mtime

    if breaker is not None and breaker.should_skip_live():
        # BUI-535: the breaker overrides --force too — once tripped, no more
        # live SerpApi charges for the rest of this batch. Still serve a
        # cache hit if one exists ("breaker-tripped mode must still allow
        # cache reads") — re-check here since the `not force` branch above
        # may have skipped the cache lookup entirely (force=True).
        if not cache_checked:
            cached, cached_mtime = _cache_get(path, ttl_sec)
            if cached is not None:
                return cached, True, cached_mtime
        raise BreakerTrippedError(
            "SerpApi circuit breaker tripped (BUI-535) — no cache entry for "
            "this query; skipping the live fetch for the rest of this batch."
        )

    def _on_retry_attempt(attempt, resp, exc):
        # BUI-535: every physical charge (interim or terminal) reports to the
        # breaker — retry_request only invokes this hook when about to retry,
        # which by construction is always an error signal.
        if breaker is not None:
            breaker.record_error()
        if record_attempt is None:
            return
        # BUI-537: translate this superseded attempt into a trail entry. A
        # retryable-status response has no exception object yet (retry_request
        # only inspects status_code, never calls raise_for_status() itself) —
        # synthesize the same HTTPError raise_for_status() would raise so the
        # recorded outcome/class matches exactly what the terminal path would
        # have recorded had THIS attempt been the last one.
        if exc is not None:
            record_attempt(f"error:{type(exc).__name__}", str(exc))
        else:
            try:
                resp.raise_for_status()
            except requests.exceptions.RequestException as http_exc:
                record_attempt(f"error:{type(http_exc).__name__}", str(http_exc))

    # BUI-333: retry/backoff routed through the shared ebay_fetch.retry_request()
    # helper rather than the hand-rolled loop this used to have. retry_request()
    # only classifies retryable vs. non-retryable *status codes* — it never calls
    # resp.raise_for_status() itself — so raise_for_status() is still called
    # explicitly below, reproducing the original "raise HTTPError on any non-2xx"
    # behavior exactly, including an un-retried 4xx raising immediately. One
    # intentional widening: the original narrowly caught (requests.Timeout,
    # requests.ConnectionError) as the retryable network-error types;
    # retry_request's retry_network_errors=True catches any
    # requests.exceptions.RequestException, matching the broader catch
    # get_token()/fetch_item_with_status() already use in ebay_fetch.py. The
    # extra types that catch admits (TooManyRedirects, ChunkedEncodingError,
    # ...) can't arise from this internally-built URL in practice; treating
    # them as retryable rather than immediately fatal is strictly safer.
    try:
        resp = retry_request(
            lambda: requests.get(request_url(canonical, api_key), timeout=SERPAPI_TIMEOUT_SEC),
            retries=FETCH_MAX_RETRIES,
            is_retryable_status=lambda code: code == 429 or code >= 500,
            retry_network_errors=True,
            on_attempt=_on_retry_attempt,
        )
    except RetryExhausted as exc:
        if exc.network_error is not None:
            # BUI-535: this IS the terminal attempt (on_attempt above is never
            # called for it) — record it here, exactly once, before re-raising.
            if breaker is not None:
                breaker.record_error()
            raise exc.network_error from exc
        # Retries exhausted on a persistently retryable (429/5xx) status —
        # fall through to the same raise_for_status() call below, which
        # raises the equivalent HTTPError (same status/message a caller
        # would have seen from the original hand-rolled loop). breaker.
        # record_error() for THIS terminal attempt happens there (not here)
        # to avoid double-counting it.
        resp = exc.response

    try:
        resp.raise_for_status()

        data = resp.json()

        # BUI-628/KTD12: capture the raw body BEFORE deciding whether it's
        # shape-valid — a response that fails our own checks (an "error" key,
        # a dropped LH_Sold=1) is exactly the provider-drift signal tier 0
        # exists to preserve, and it was previously discarded here. Compute
        # the validation outcome first, capture unconditionally with it, THEN
        # raise if invalid — a network error with no body (raise_for_status()
        # above, or resp.json() itself) never reaches this point, so it still
        # captures nothing, unchanged from before.
        validation_error = None
        if "error" in data:
            validation_error = f"SerpApi error: {data['error']}"
        else:
            # Verify the eBay URL actually has LH_Sold=1 — SerpApi silently
            # drops LH_* params if you pass them directly, and a missing sold
            # filter returns active listings (FMV will be wrong, typically
            # far too low).
            ebay_url = data.get("search_metadata", {}).get("ebay_url", "")
            if "LH_Sold=1" not in ebay_url:
                validation_error = (
                    f"Sold filter not applied — eBay URL missing LH_Sold=1.\n"
                    f"  ebay_url={ebay_url}\n"
                    f"  query={nkw}\n"
                    "Use show_only=Sold (LH_Sold=1 / LH_Complete=1 are silently dropped)."
                )
        _capture_raw_response(
            PROVIDER_SERPAPI, nkw, canonical, data,
            validation=validation_error or "ok",
        )
        if validation_error is not None:
            raise SerpApiError(validation_error)
    except (SerpApiError, requests.RequestException):
        if breaker is not None:
            breaker.record_error()
        raise

    if breaker is not None:
        breaker.record_success()

    fetched_at = time.time()
    _cache_put(path, data)
    return data, False, fetched_at


# ─── Secondary provider fetch + failover orchestration (BUI-545) ─────────────

def _verify_sold_comps_shape(nkw: str, data: dict) -> None:
    """R11-style structural sold-verification for a sold-comps.com response.

    The analog of fetch()'s LH_Sold=1 check: every item in a sold=true
    response must be sold-SHAPED — listingType "sold" with a soldPrice and an
    endedAt. One active listing blended into a comp pool corrupts FMV
    silently (the generalized LH_Sold=1 silently-dropped trap), so any
    violation fails the WHOLE response loudly as a provider error rather than
    filtering quietly. The 2026-07-28 smoke test measured 0/79 violations
    across sold responses (and 50/50 structurally distinct items in active
    mode); if strict-any ever proves flaky, loosening it is a deliberate
    follow-up decision, not a default.
    """
    if "error" in data:
        raise SoldCompsError(f"sold-comps.com error: {data['error']}")
    items = data.get("items") or []
    bad = [i for i in items
           if i.get("listingType") != "sold"
           or not i.get("soldPrice") or not i.get("endedAt")]
    if bad:
        raise SoldCompsError(
            f"Sold shape violated — {len(bad)}/{len(items)} item(s) in a "
            "sold=true sold-comps.com response are not sold-shaped "
            f"(listingType/soldPrice/endedAt).\n  query={nkw}"
        )


# BUI-701: a much longer, jittered backoff specifically for a 429 from
# sold-comps.com. The default retry_request() schedule (2**attempt: 2s/4s/8s)
# is right for an ordinary transient (a 5xx blip, a network error) but wrong
# here: sold-comps.com's ceiling is exactly the rate this module dispatches
# at (see _SOLD_COMPS_RATE_LIMIT_PER_MIN), so a 429 is not noise — it is live
# evidence the request window is currently saturated. Retrying 2s later lands
# the retry back in that SAME saturated window; this was the mechanism that
# turned the 2026-08-07 outage's initial 429s into a self-reinforcing storm
# that fed itself until the breaker tripped (109 queries short-circuited).
# This base is sized to comfortably outlast one whole rate-limiter interval
# many times over (one interval = 60s / 48 req-per-min ~= 1.25s — 15s is
# >10x that), with up to 50% jitter so the up-to-4 semaphore-holding threads
# that can all 429 in the same saturated instant don't retry in lockstep and
# recreate the very burst they're recovering from.
_SOLD_COMPS_429_BASE_BACKOFF_SEC = 15.0


def _sold_comps_backoff_seconds(attempt: int, resp, exc) -> float:
    """backoff_seconds callback for fetch_sold_comps()'s retry_request() call
    (BUI-701) — see _SOLD_COMPS_429_BASE_BACKOFF_SEC for why a 429
    specifically gets a different, longer schedule than everything else
    (an ordinary 5xx/network transient keeps the shared 2**attempt default)."""
    if resp is not None and resp.status_code == 429:
        base = _SOLD_COMPS_429_BASE_BACKOFF_SEC * (2 ** attempt)
        return base + random.uniform(0, base * 0.5)
    return float(2 ** attempt)


def fetch_sold_comps(nkw: str, api_key: str, *, force: bool = False,
                     ttl_sec: int = DEFAULT_CACHE_TTL_SEC,
                     record_attempt=None,
                     breaker: "_CircuitBreaker | None" = None,
                     ) -> tuple[dict, bool, float]:
    """Fetch a sold-comps.com response with caching. Returns
    (data, cache_hit, response_fetched_at).

    Mirrors fetch()'s contract (own cache key → breaker gate → live with the
    same retry policy) with one deliberate asymmetry: this breaker counts
    TERMINAL failures only, never interim retry attempts. sold-comps.com
    rate-limits at 60 req/min, so a concurrent batch can 429 transiently and
    recover on the existing backoff — an interim 429 is neither charged nor
    outage evidence, and counting it would trip the breaker spuriously on any
    large batch. (SerpApi's breaker counts every attempt because every
    attempt IS a charge there.) `record_attempt` still fires for superseded
    retry attempts — the trail stays a full attempt trail (BUI-537); trail
    recording and breaker accounting are simply decoupled here.

    BUI-657/KTD7: `response_fetched_at` mirrors fetch()'s contract — the
    cache entry's mtime on a hit, `time.time()` at successful validation on a
    live fetch.
    """
    canonical = canonical_sold_comps_url(nkw)
    path = _cache_path(canonical)

    cache_checked = False
    if not force:
        cached, cached_mtime = _cache_get(path, ttl_sec)
        cache_checked = True
        if cached is not None:
            return cached, True, cached_mtime

    if breaker is not None and breaker.should_skip_live():
        # Same force-does-not-bypass-the-breaker semantics as fetch().
        if not cache_checked:
            cached, cached_mtime = _cache_get(path, ttl_sec)
            if cached is not None:
                return cached, True, cached_mtime
        raise BreakerTrippedError(
            "sold-comps.com circuit breaker tripped (BUI-545) — no cache "
            "entry for this query; skipping the live fetch for the rest of "
            "this batch."
        )

    def _on_retry_attempt(attempt, resp, exc):
        # Terminal-failures-only breaker: interim retries are deliberately
        # NOT reported to it (see docstring) — trail recording only.
        if record_attempt is None:
            return
        if exc is not None:
            record_attempt(f"error:{type(exc).__name__}", str(exc))
        else:
            try:
                resp.raise_for_status()
            except requests.exceptions.RequestException as http_exc:
                record_attempt(f"error:{type(http_exc).__name__}", str(http_exc))

    def _paced_get():
        # BUI-701: pace BEFORE acquiring the semaphore, and scope the
        # semaphore to only the HTTP call itself — not retry_request()'s
        # backoff sleep between attempts. A thread waiting on either the
        # rate limiter or a 429's (now much longer) backoff must not sit on
        # one of the 4 concurrency slots the whole time it waits, or it
        # starves the other 3 workers of a slot they could be using right
        # now. See _RateLimiter's docstring for the same reasoning applied
        # to its own wait. This also means every RETRY (not just the first
        # attempt) is itself paced — a retry is a fresh request against the
        # same ceiling, not exempt from it.
        _SOLD_COMPS_RATE_LIMITER.acquire()
        with _SOLD_COMPS_SEMAPHORE:
            return requests.get(
                canonical,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=SOLD_COMPS_TIMEOUT_SEC,
            )

    try:
        resp = retry_request(
            _paced_get,
            retries=FETCH_MAX_RETRIES,
            is_retryable_status=lambda code: code == 429 or code >= 500,
            retry_network_errors=True,
            on_attempt=_on_retry_attempt,
            backoff_seconds=_sold_comps_backoff_seconds,
        )
    except RetryExhausted as exc:
        if exc.network_error is not None:
            if breaker is not None:
                breaker.record_error()
            raise exc.network_error from exc
        resp = exc.response

    try:
        resp.raise_for_status()
        data = resp.json()

        # BUI-628/KTD12: capture ahead of the shape check — mirrors fetch()'s
        # matching comment above. A shape-invalid body is provider-drift
        # evidence tier 0 exists to keep, tagged with the validation outcome
        # instead of being silently discarded as it was before. A non-2xx or
        # unparseable body never reaches this point, so it still captures
        # nothing, unchanged from before.
        validation_error = None
        try:
            _verify_sold_comps_shape(nkw, data)
        except SoldCompsError as exc:
            validation_error = str(exc)
        _capture_raw_response(
            PROVIDER_SOLD_COMPS, nkw, canonical, data,
            validation=validation_error or "ok",
        )
        if validation_error is not None:
            raise SoldCompsError(validation_error)
    except (SerpApiError, requests.RequestException):
        # (SoldCompsError subclasses SerpApiError.) A terminal failure — the
        # one kind this provider's breaker counts. Covers a non-2xx after
        # retries (incl. 401 bad key / 403 quota exhausted), an unparseable
        # body, and a shape violation.
        if breaker is not None:
            breaker.record_error()
        raise

    if breaker is not None:
        breaker.record_success()

    fetched_at = time.time()
    _cache_put(path, data)
    return data, False, fetched_at


def _fetch_with_fallback(nkw: str, api_key: str, *, force: bool = False,
                         ttl_sec: int = DEFAULT_CACHE_TTL_SEC, page: int = 1,
                         record_attempt=None,
                         breaker: "_CircuitBreaker | None" = None,
                         sold_comps_key: str | None = None,
                         sold_comps_breaker: "_CircuitBreaker | None" = None,
                         providers: tuple = DEFAULT_PROVIDER_ORDER,
                         ) -> tuple[dict, bool, str, float]:
    """Run one query through the provider chain (BUI-545). Returns
    (data, cache_hit, provider, response_fetched_at): `provider` names who
    served, so the caller can parse the raw response with the right
    extractor and tag the trail; `response_fetched_at` (BUI-657/KTD7) is
    whichever provider's own fetch time — cache mtime on a hit, live fetch
    time otherwise.

    Failover fires on EXCEPTION only — a 200 with zero results from either
    provider is a genuine n=0 (BUI-536's error-vs-empty distinction), never
    a second-provider probe: anything else would double-spend on every
    legitimately thin vintage query. A provider failure that a further
    provider supersedes is recorded through `record_attempt(outcome, detail,
    provider)` — exactly the BUI-537 superseded-attempt semantics, extended
    with a provider tag. The LAST provider's failure is NOT recorded here:
    the caller learns it from the raised exception (which carries a
    `.provider` attribute for its trail entry), so recording it too would
    double-count. A no-key run degrades to the SerpApi-only chain, byte-for-
    byte pre-BUI-545 behavior.
    """
    active = []
    for p in providers:
        if p == PROVIDER_SOLD_COMPS:
            if not sold_comps_key:
                continue
            if page > 1:
                # BUI-523 pagination is SerpApi-shaped; one SOLD_COMPS_COUNT
                # request already covers what a page 2 would add.
                continue
        active.append(p)
    if not active:
        raise SerpApiError(
            "no sold-comps provider available for this query "
            f"(providers={providers!r}, page={page})"
        )

    last_exc: Exception | None = None
    for i, provider in enumerate(active):
        if record_attempt is None:
            hook = None
        else:
            # Bind this provider into the 2-arg hook fetch()/fetch_sold_comps()
            # expect, so their interim-retry entries carry the provider tag too.
            def hook(outcome, detail, _provider=provider):
                record_attempt(outcome, detail, _provider)
        try:
            if provider == PROVIDER_SERPAPI:
                data, cache_hit, response_fetched_at = fetch(
                    nkw, api_key, force=force, ttl_sec=ttl_sec, page=page,
                    record_attempt=hook, breaker=breaker,
                )
            else:
                data, cache_hit, response_fetched_at = fetch_sold_comps(
                    nkw, sold_comps_key, force=force, ttl_sec=ttl_sec,
                    record_attempt=hook, breaker=sold_comps_breaker,
                )
            return data, cache_hit, provider, response_fetched_at
        except (SerpApiError, requests.RequestException) as e:
            e.provider = provider
            if i == len(active) - 1:
                if last_exc is not None:
                    raise e from last_exc
                raise
            if record_attempt is not None:
                record_attempt(f"error:{type(e).__name__}", str(e), provider)
            last_exc = e
    raise AssertionError("unreachable: the loop always returns or raises")


# ─── Hard excludes ────────────────────────────────────────────────────────────
#
# BUI-269: the lot/reprint/foreign-edition/trading-card checks that used to
# live in this regex are now sourced from comic_identity.is_comp_excluded()
# (apps/ebay/src/comic_identity.py) — that module is the single source of
# truth, reconciling this lexicon with the near-identical one seller_scan.py
# used to hand-maintain (BUI-253). What remains here is condition/grading/
# damage exclusion with no analog in comic_identity: it isn't about comic
# *identity* at all, so it stays local to the FMV comp pipeline.

# BUI-668: the signature branch is a bare word-bounded `signed`, not `signed by`.
# `signed by` let a bare "Signed <name>" copy into the pool — a signed/COA copy
# is not comparable to a raw one, and the class is systematically dearer than the
# pools it lands in (measured: 2.22x the pool median, 84 of 99 members above it).
# The word boundary is load-bearing and does most of the precision work on its
# own: it rejects "DESIGN"/"Designs", "unsigned", and "METAL SIGN". The one
# residual false positive the corpus holds is a seller advertising a book as
# "NOT signed", hence the fixed-width negative lookbehind.
#
# Measured over the offline corpus at ~/.cache/ebay-sold-comps (518 cached
# responses, 483 of them yielding a priced pool) — BUI-668: agrees with the
# hand-labelled class on all 13,876 surviving comps at 0 disagreements, excludes
# 99 comps, re-admits 0 (so dropping `signed\s+by` regresses nothing), and moves
# max_bid DOWN 12 / UP 4 across the CGC ladder — net -$45, the cap-protecting
# direction. It is the first class in this ticket sequence to register on the
# sharp test (8 members above their pool's Q75 at >=3x its median; BUI-629 and
# BUI-667 both scored 0), including a $132.50 comp at 18.9x its pool median.
# The three BUI-603 sentinel books are unaffected (depth 105->103 / 62->62 /
# 88->88, all three medians unchanged), so the probe's bounds do not move.
#
# NOT fixed here, deliberately: `restored` also matches inside "unrestored".
# That false positive is real and precision 1.000 fixable via `(?<!un)restored`,
# but it was measured at 1 of 483 priced pools moving, UP +28.6% (max_bid
# $60 -> $70), net +$95 cap-RAISING across the ladder. A correct-on-identity fix that
# only raises caps buys no downside protection — see BUI-668 and
# docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-classifier.md
# ("Which side of the pool median does the class sit on?"). Do not "fix" it
# without re-measuring.
LOCAL_EXCLUDE_RE = re.compile(
    r'''
    coverless | no\s+cover | cover\s+torn | cvr\s+off | detached\s+cover |
    missing\s+pages? | missing\s+pin | missing\s+wrap |
    vol[\s.]?[2-9] | \bv[2-9]\b |
    \bpsa\b | \bpgx\b |
    (?<!not\s)\bsigned\b | \bautograph | stan\s+lee.*sign | signature\s+series |
    ww\s+live\s+sale | space\s+filler | restored | water.?stain
    ''',
    re.IGNORECASE | re.VERBOSE,
)


def hard_exclude(title: str) -> bool:
    return comic_identity.is_comp_excluded(title) or bool(LOCAL_EXCLUDE_RE.search(title))


# ─── Grade parsing ────────────────────────────────────────────────────────────

# Fixed numeric regex: covers the full CGC scale including 9.2/9.4/9.6/9.9.
# The previous form `\b([0-9]\.[058])\b` silently dropped those.
#
# BUI-183: exclude price/measurement context.
#   Negative lookbehinds (fixed-width):
#     (?<!\$)  — reject when preceded by a dollar sign (price: $9.5)
#     (?<!x )(?<!X )  — reject when preceded by "x " (second number in a
#                       dimension pair: 2.5 x 3.5); requires exactly one space
#                       so "X-Men" (hyphen, not space) is unaffected.
#   Negative lookahead:
#     (?!\s*(?:in(?:ch(?:es?)?)?\b|cm\b|mm\b|lbs?\b|oz\b|x\b|ship(?:ping)?\b|["']))
#     — reject when the number is immediately followed (past optional whitespace)
#       by a measurement or shipping unit.  `x\b` catches the first number in a
#       dimension pair ("2.5 x"); word boundary on each unit prevents false
#       matches inside longer words.
_NUMERIC_GRADE_RE = re.compile(
    r'(?<!\$)(?<!x )(?<!X )'
    r'\b([0-9]\.[02-9])'
    r'(?!\w)'  # restore the original trailing boundary: a digit/letter immediately
               # after (e.g. "9.50", "5.50 dollars") is a price/number, not a grade
    r'(?!\s*(?:in(?:ch(?:es?)?)?\b|cm\b|mm\b|lbs?\b|oz\b|x\b|ship(?:ping)?\b|["\']))'
)

# Letter combos — most specific first. Order matters: slash-combos (e.g.
# VF/NM) must be checked before their single-letter components (NM), since
# `\bnm\b` would otherwise match inside "VF/NM" and short-circuit the loop.
#
# Boundary note: `\b` requires a word↔non-word transition. For patterns
# ending in non-word characters like `+` or `-`, a trailing `\b` fails when
# the next char is whitespace or end-of-string (both non-word). Use `(?!\w)`
# for trailing boundaries on non-word tails.
_LETTER_PATTERNS = [
    # Tier 1 — slash combos (longest first)
    (re.compile(r'\bnm[/\\]m\b', re.I), 9.6),
    (re.compile(r'\bvf[/\\]nm\b', re.I), 9.0),
    (re.compile(r'\bfn[/\\]vf\b|\bfine[/\\]vf\b|\bfvf\b', re.I), 7.0),
    (re.compile(r'\bvg[/\\]fn\+(?!\w)', re.I), 5.5),
    (re.compile(r'\bvg[/\\]fn\b', re.I), 5.0),
    (re.compile(r'\bgd[/\\]vg\b', re.I), 3.0),
    (re.compile(r'\bfr[/\\]gd\b', re.I), 1.5),

    # Tier 2 — letter + modifier (+ / -)
    (re.compile(r'\bnm\+(?!\w)', re.I), 9.6),
    (re.compile(r'\bnm-(?!\w)', re.I), 9.2),
    (re.compile(r'\bvf\+(?!\w)', re.I), 8.5),
    (re.compile(r'\bvf-(?!\w)', re.I), 7.5),
    (re.compile(r'\bfn\+(?!\w)|\bfine\+(?!\w)', re.I), 6.5),
    (re.compile(r'\bfn-(?!\w)|\bfine-(?!\w)', re.I), 5.5),
    (re.compile(r'\bvg\+(?!\w)', re.I), 4.5),
    (re.compile(r'\bvg-(?!\w)', re.I), 3.5),
    (re.compile(r'\bgd\+(?!\w)', re.I), 2.5),

    # Tier 3 — bare letters (must come last; other patterns would match inside)
    (re.compile(r'\bnm\b(?![+\-/\\])', re.I), 9.4),
    (re.compile(r'\bvf\b(?![+\-/\\])', re.I), 8.0),
    (re.compile(r'\bfn\b(?![+\-/\\])|\bfine\b(?![+\-/\\])', re.I), 6.0),
    (re.compile(r'\bvg\b(?![+\-/\\])|\bvery good\b', re.I), 4.0),
    (re.compile(r'\bgd\b(?![+\-/\\])|\bgood\b', re.I), 2.0),
    (re.compile(r'\bfr\b(?![+\-/\\])|\bfair\b', re.I), 1.0),
    (re.compile(r'\bpoor\b', re.I), 0.5),
]


def parse_grade(title: str) -> float | None:
    """Extract a numeric CGC-scale grade from a listing title, or None."""
    m = _NUMERIC_GRADE_RE.search(title)
    if m:
        v = float(m.group(1))
        if 0.5 <= v <= 10.0:
            return v
    for pattern, value in _LETTER_PATTERNS:
        if pattern.search(title):
            return value
    return None


# ─── Comp parsing ─────────────────────────────────────────────────────────────

def _parse_price(raw) -> float | None:
    if raw is None:
        return None
    s = re.sub(r'[^\d.]', '', str(raw).replace(',', ''))
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0.50 <= v <= 50000 else None


# BUI-675: SerpApi's `price.extracted` is a naked float with the currency
# symbol already stripped, so a foreign-currency sold listing used to enter the
# priced pool at FACE VALUE. The proof was one eBay listing (267656830968, New
# X-Men #128) cached twice: `{"raw": "R$ 101.77", "extracted": 101.77}` and
# `{"raw": "$19.99", "extracted": 19.99}` — the same sale, once in Brazilian
# Real, once in USD. Taken as dollars that is a 5.09x inflation of a $19.99
# comp. It only ever surfaced because that listing happened to be fetched
# twice; a book seen ONCE in a foreign currency is silently wrong.
#
# `price.raw` is the only place the currency survives, so it — not `extracted` —
# is the authority on whether the number is dollars. Measured over the offline
# corpus at ~/.cache/ebay-sold-comps (518 responses, 10,644 SerpApi results
# carrying a price): `raw` and `extracted` are always present TOGETHER (10,644
# both / 0 with only one), so gating on `raw` costs nothing today and cannot
# regress a result that has a price. Rejecting a missing `raw` is therefore
# fail-closed on evidence, not on a guess: with no currency string we have no
# reason to believe a number is dollars, and mispricing a cap costs money where
# dropping a comp only thins a pool.
#
# The predicate is an ALLOWLIST, not a "contains $" test: `R$`, `C $`, `AU $`,
# `HK$`, `CA$`, `NZ$` and `S$` all carry a dollar sign and none of them are USD.
# Only a bare `$` or an explicit `US $` qualifier passes, and the pattern is
# anchored at BOTH ends so a trailing currency code (`$19.99 CAD`) cannot slip
# through on the strength of its prefix. Over the corpus this accepts 10,618
# and rejects 26 — the 22 `R$` and 4 `RMB` results, which are 100% of two
# responses ("New X-Men 128", "Uncanny X-Men 311"): the flip happens per
# RESPONSE, not per listing.
#
# Deliberately NOT allowed: a trailing `USD` (`"$19.99 USD"`). It is a genuine
# USD format but has never appeared in the corpus, and its failure mode is a
# dropped comp, not a wrong cap. Do not widen without re-measuring.
#
# No FX conversion — an exchange rate at parse time is not the rate at sale
# time, and one comp is not worth that dependency.
_USD_PRICE_RAW_RE = re.compile(r'^\s*(?:US\s*)?\$\s*[\d,]*\.?\d+\s*$', re.IGNORECASE)


def _is_usd_serpapi_price(price_obj: dict) -> bool:
    """True when SerpApi's price object proves the amount is US dollars."""
    raw = price_obj.get("raw")
    return isinstance(raw, str) and bool(_USD_PRICE_RAW_RE.match(raw))


def _is_non_usd_serpapi_result(result: dict) -> bool:
    """BUI-678: True when a SerpApi organic_result is the specific shape
    `parse_comp()` drops for FAILING the BUI-675 currency gate — a real
    listing (title + a price object present) that `_is_usd_serpapi_price`
    rejects — as opposed to a listing `parse_comp()` drops for an unrelated
    reason (no title at all, or a price that fails to parse after passing
    the gate). Callers must only consult this AFTER `parse_comp(result)` has
    already returned None, so a comp that priced normally is never
    double-counted as a currency drop.

    This exists because `parse_comp()`'s `dict | None` contract is relied on
    by its own tests (asserting the parsed shape or a bare None) and by nothing
    else needing a rejection *reason* until now — duplicating the two guards
    here, rather than widening that return type, keeps the parser's contract
    unchanged."""
    if not result.get("title"):
        return False
    price_obj = result.get("price") or {}
    if not price_obj:
        return False
    return not _is_usd_serpapi_price(price_obj)


def parse_comp(result: dict) -> dict | None:
    """Convert a SerpApi organic_result into our normalized comp shape."""
    title = result.get("title", "")
    if not title:
        return None
    product_id = str(result.get("product_id") or result.get("item_id") or "")
    price_obj = result.get("price") or {}
    # BUI-675: currency gate BEFORE the price parse — `extracted` is only
    # trusted once `raw` has vouched that the number is dollars.
    if not _is_usd_serpapi_price(price_obj):
        return None
    price = _parse_price(price_obj.get("extracted") or price_obj.get("raw"))
    if price is None:
        return None
    return {
        "product_id": product_id,
        "title": title,
        "price": price,
        "grade": parse_grade(title),
        "sold_date": result.get("sold_date", ""),
        "buying_format": result.get("buying_format", ""),
        "link": result.get("link", ""),
    }


_ITM_ID_RE = re.compile(r"/itm/(\d+)")


def parse_comp_sold_comps(item: dict) -> dict | None:
    """Convert a sold-comps.com item into the same normalized comp shape
    parse_comp() emits (BUI-545).

    `itemId` is the eBay /itm/ number — the SAME namespace SerpApi's
    `product_id` carries (verified against the pre-wall cache) — so
    cross-provider dedupe within a book's seen_ids and BUI-160
    self-exclusion both work without translation; the URL regex is only a
    fallback. `soldPrice` (not totalPrice or bestOfferAccepted) matches
    SerpApi's price.extracted semantics — 66/70 exact matches in the BUI-545
    fidelity test. `soldPrice` is the actual accepted amount, including on an
    OBO sale — sold-comps.com's own documentation says so verbatim, and
    BUI-552 confirmed it experimentally (OBO share ~22.3% of the sample, no
    price bias measured). `bestOfferAccepted` is a boolean badge flag only;
    the item schema has no separate accepted-amount field at all. BUI-552
    closed that option — this is settled, not an open question.
    `endedAt` (ISO YYYY-MM-DD) passes through verbatim:
    fmv_math._parse_sold_date already accepts ISO-8601.

    BUI-675 correction: `soldPrice` IS a bare number, but this provider is not
    currency-blind the way that suggests — every item also carries an explicit
    `soldCurrency`, and it reads 'USD' on 6,192 of 6,192 items in the offline
    corpus, including items shipping from the UK, Canada, Australia and China.
    So sold-comps.com normalizes to dollars and has none of the face-value bug
    parse_comp had. The guard below is consequently a measured NO-OP today; it
    exists so the primary provider is not the unguarded one if that ever
    changes. It rejects only a currency that is PRESENT and not USD: a missing
    field — or a blank one, which is the same absence of a declaration — is
    treated as USD, deliberately unlike parse_comp. The asymmetry is
    the blast radius, not a different standard of evidence — for SerpApi the
    currency lives inside the price string, so its absence means we never had a
    price string to trust, whereas here it is an independent field whose
    disappearance would black out the DEFAULT PRIMARY provider (and a
    provider-wide blackout surfaces as a clean n=0, the invisible failure class
    of BUI-565). Two independent schema changes are needed to slip a foreign
    price past this; one would be needed to break FMV entirely.
    """
    title = item.get("title", "")
    if not title:
        return None
    currency = str(item.get("soldCurrency") or "").strip()
    if currency and currency.upper() != "USD":
        return None
    m = _ITM_ID_RE.search(item.get("url") or "")
    product_id = str(item.get("itemId") or (m.group(1) if m else ""))
    price = _parse_price(item.get("soldPrice"))
    if price is None:
        return None
    return {
        "product_id": product_id,
        "title": title,
        "price": price,
        "grade": parse_grade(title),
        "sold_date": item.get("endedAt", ""),
        "buying_format": item.get("buyingFormat", ""),
        "link": item.get("url", ""),
    }


def _is_non_usd_sold_comps_item(item: dict) -> bool:
    """BUI-678: sold-comps.com counterpart of `_is_non_usd_serpapi_result` —
    True when a raw item is the specific shape `parse_comp_sold_comps()`
    drops for its currency guard (a titled item whose `soldCurrency` is
    present and not USD), not for an unrelated reason. Measured as a no-op
    today (see that guard's docstring — 6,192/6,192 USD in the offline
    corpus) but kept so the count stays honest if that ever changes. Callers
    must only consult this AFTER `parse_comp_sold_comps(item)` has already
    returned None."""
    if not item.get("title"):
        return False
    currency = str(item.get("soldCurrency") or "").strip()
    return bool(currency) and currency.upper() != "USD"


def _has_next_page(data: dict) -> bool:
    """True when SerpApi's response indicates a further result page exists.

    BUI-523: SerpApi's eBay engine echoes both eBay's own `pagination` object
    and its own `serpapi_pagination` mirror; the latter's `next` field is the
    authoritative "is page 1 full, i.e. does page 2 exist" signal. This
    trusts SerpApi's own pagination resolution rather than reverse-engineering
    a "full page" guess from a raw result-count threshold (eBay's per-page
    count isn't a fixed constant we control, since `_ipg` is left at its
    default). A missing/empty pagination object fails CLOSED — no page-2
    fetch, no extra SerpApi spend — rather than guessing there's more.
    """
    pagination = data.get("serpapi_pagination") or {}
    return bool(pagination.get("next"))


# A genuine slab listing names its certifier (CGC/CBCS) in the title. BUI-524's
# inclusive tier drops the graded excludes, so its results are a MIX of raw and
# slab listings; this is how the two are told apart afterward. Mirrors
# apps/fmv/src/fmv_runner.py's `_SLAB_TITLE_RE`/`_slab_comps_only` — duplicated
# across the package boundary rather than shared, per this repo's existing
# convention (comic-fmv shells out to ebay-sold-comps rather than importing it;
# see CLAUDE.md's "FMV pipeline shells out across package boundaries").
_SLAB_TITLE_RE = re.compile(r"\b(?:cgc|cbcs)\b", re.IGNORECASE)


def _is_slab_comp(comp: dict) -> bool:
    """True for a genuine CGC/CBCS certified-slab comp: grade + price parsed
    AND the certifier named in the title (BUI-524). A comp that merely carries
    a grade token ("… FN 6.0 …") without a certifier name is raw, not a slab."""
    return (comp.get("grade") is not None and comp.get("price") is not None
            and bool(_SLAB_TITLE_RE.search(comp.get("title") or "")))


# ─── Per-book pipeline (three-tier query strategy) ───────────────────────────

def fetch_book_comps(book: dict, api_key: str, *, force: bool = False,
                     ttl_sec: int = DEFAULT_CACHE_TTL_SEC,
                     breaker: "_CircuitBreaker | None" = None,
                     sold_comps_key: str | None = None,
                     sold_comps_breaker: "_CircuitBreaker | None" = None,
                     providers: tuple = DEFAULT_PROVIDER_ORDER) -> dict:
    """Run the three-tier query strategy for one book.

    1. Base query (always): "title issue" year publisher
    2. Auto-broaden (if base <5 results): drop year
    3. Grade-targeted (if <10 grade-tagged comps after parsing base): add grade label

    Tiers 2 and 3 are conditional. Most modern books need only tier 1.

    BUI-523: after tier 1, a gated page-2 fetch of the SAME base query fires
    ONLY when SerpApi confirms page 1 was full (a next page exists — see
    _has_next_page) AND the comp pool is still short on grade-tagged comps
    (< GRADE_TAGGED_THRESHOLD) — i.e. only when a liquid book's comp supply
    is genuinely capped by the single-page fetch AND the extra page is
    likely to help. A thin (non-full) page 1 — the common vintage-book case
    — never has a next page, so it costs zero extra SerpApi searches. Capped
    at exactly one extra page (never page 3+) to bound spend against the
    250/month SerpApi quota.

    BUI-524: after tiers 1-3, a conditional 4th "inclusive" tier fires ONLY for
    a vintage book (pre-_VINTAGE_YEAR_CUTOFF) whose raw pool is still thin
    (< THIN_RESULTS_THRESHOLD comps) on a normal (non-`include_graded`) call.
    It re-queries WITHOUT the `-cgc -cbcs -graded -slab` excludes and splits the
    results via `_is_slab_comp`: raw candidates join the same `comps` pool
    everything else does, while genuine slab comps are kept separately in
    `slab_comps` (never blended into `comps` — a slab price would drag the raw
    pool's median up). This gets a vintage key BOTH its raw comps and a slab
    ladder from ONE extra query — feeding BUI-529's always-on cross-check
    without a dedicated second graded-only pass in the common case. The common
    case (modern/liquid book, or a vintage book whose first three tiers already
    found >= THIN_RESULTS_THRESHOLD raw comps) fires ZERO extra searches, so
    `comps` composition is byte-identical to pre-BUI-524 output whenever this
    tier doesn't fire.

    BUI-581: between tiers 2 and 3 a vintage book whose masthead has a known
    RENAME counterpart (see `_MASTHEAD_RENAME_PAIRS`) and whose pool is still
    thin gets ONE probe under the other name; whichever pool is deeper is kept
    (swapped wholesale, never merged) and tiers 3-5 then run against that
    masthead. `masthead_swapped_to` names the winning alias, or is None when the
    caller's own title was queried — which it always is for a modern book, a
    year-less book, a title with no counterpart, or a pool that was never thin.

    BUI-588: a LAST tier fires only when the pool is exactly empty and the book
    carried a `variant` — it re-queries with the variant term dropped, because a
    collector/catalog descriptor that appears in no listing title zeroes every
    tier above and no amount of widening recovers it. Comps found this way price
    the BASE cover, so `variant_dropped` reports the substitution rather than
    letting a variant-blind number be written silently.

    BUI-537/535: every tier routes through the same `_run` closure, so every
    fetch attempt — including retry attempts that fire *inside* `fetch()` and
    the always-on cross-check/inclusive tier — gets the same trail recording
    and the same circuit-breaker gating. The whole body below is wrapped in a
    try/except so an unexpected exception (e.g. a malformed `book`) still
    returns the partial `queries_used`/`comps`/`slab_comps` trail gathered so
    far, tagged with `error`, instead of losing it — `run_batch()`'s own
    per-book exception boundary used to zero this to `[]` because a raised
    exception never reached this function's normal `return` below.

    BUI-545: every tier also routes through `_fetch_with_fallback`, so each
    query runs the provider chain — sold-comps.com primary by default (see
    DEFAULT_PROVIDER_ORDER), SerpApi as the fallback tier — failing over
    per query on error when `sold_comps_key` is provided (see that
    function's docstring). Every
    `queries_used` entry now carries a `provider` tag; `breaker_tripped` in
    the output is the OR of both providers' breakers ("this book was affected
    by an outage" keeps its meaning for consumers). Default kwargs (no key,
    DEFAULT_PROVIDER_ORDER) reproduce pre-BUI-545 behavior exactly.
    """
    queries_used: list[dict] = []
    seen_ids: set[str] = set()
    comps: list[dict] = []
    # BUI-524: populated only when the tier-4 inclusive pass fires; a genuine
    # CGC/CBCS slab comp lands here instead of `comps` (see `_is_slab_comp`).
    slab_comps: list[dict] = []
    # BUI-581 / BUI-588: bound here (not inside the try) so the error return at
    # the bottom carries the same keys as the success return.
    masthead_swapped_to: str | None = None
    variant_dropped: str | None = None
    # BUI-678: comps the BUI-675 currency gate rejected (title present, price
    # object present, currency proven non-USD) — summed across every tier's
    # `_run` call below. A response that loses ALL its comps to this gate is a
    # successful, non-empty `queries_used` that still classifies as a genuine
    # n=0 to `_is_fetch_error` (deliberately — see that function's docstring;
    # this is NOT a fetch failure). This total is how a caller tells that
    # shape apart from a book that genuinely has no sold history.
    non_usd_dropped_total = 0

    def _breaker_tripped() -> bool:
        # BUI-545: OR of both providers' breakers — either one tripping means
        # this book was (at least partly) served under outage conditions.
        return bool(breaker is not None and breaker.tripped) or bool(
            sold_comps_breaker is not None and sold_comps_breaker.tripped)

    try:
        title = book["title"]
        # BUI-581: `title` is rebound below when the alt-masthead tier wins, so
        # keep the caller's own string for the echoed `input` block — that field
        # is a contract ("here is what you asked for"), not a record of which
        # name we ended up querying (`masthead_swapped_to` is that record).
        input_title = title
        issue = str(book["issue"])
        # BUI-565: coerce the batch envelope's `year` (a STRING out of
        # /comic:identify) to int|None once, here, so every tier below — and
        # the echoed `out_input["year"]` — sees one type. See `_coerce_year`.
        year = _coerce_year(book.get("year"))
        publisher = book.get("publisher")
        variant = book.get("variant")  # BUI-304: now a query keyword, not DB-only
        self_id = str(book.get("item_id", ""))
        # BUI-348: opt-in graded-comp fetch for the CGC-proxy tier. Default
        # (field absent/falsy) keeps exclude_graded=True — every existing
        # caller's queries stay byte-for-byte identical. Only a book explicitly
        # tagged `include_graded: true` (comic-fmv's second, proxy-only pass)
        # drops the `-cgc -cbcs -graded -slab` terms so the CGC/CBCS slab
        # ladder surfaces.
        exclude_graded = not bool(book.get("include_graded"))

        if self_id:
            seen_ids.add(self_id)

        def _run(tier: str, nkw: str, *, page: int = 1, route_slabs: bool = False) -> dict:
            def _record_retry_attempt(outcome: str, detail: str,
                                      provider: str) -> None:
                # BUI-537: an attempt this call's own fetch superseded with a
                # further attempt (an internal retry, or — BUI-545 — a failed
                # provider a later provider replaced): a real charge/attempt
                # that would otherwise be invisible.
                queries_used.append({
                    "tier": tier,
                    "nkw": nkw,
                    "page": page,
                    "outcome": outcome,
                    "error": detail,
                    "provider": provider,
                })

            try:
                # BUI-523 note: page defaults to 1, so always forwarding
                # page=page is byte-identical to the old page==1 branch that
                # omitted the kwarg — collapsed now that this call also needs
                # to thread record_attempt/breaker uniformly.
                data, cache_hit, provider, response_fetched_at = _fetch_with_fallback(
                    nkw, api_key, force=force, ttl_sec=ttl_sec, page=page,
                    record_attempt=_record_retry_attempt, breaker=breaker,
                    sold_comps_key=sold_comps_key,
                    sold_comps_breaker=sold_comps_breaker,
                    providers=providers,
                )
            except (SerpApiError, requests.RequestException) as e:
                # BUI-537: page (int) and outcome are now always present,
                # including on page-1 entries — tier/nkw/error unchanged for
                # back-compat (BUI-536's _is_fetch_error keys on 'error').
                # BUI-545: provider = whose terminal failure this was
                # (attached by _fetch_with_fallback; None only for an
                # availability error raised before any provider ran).
                queries_used.append({
                    "tier": tier,
                    "nkw": nkw,
                    "page": page,
                    "outcome": f"error:{type(e).__name__}",
                    "error": str(e),
                    "provider": getattr(e, "provider", None),
                })
                return {"added": 0, "has_next_page": False}
            # BUI-545: parse the raw response with the serving provider's
            # extractor — the comp shape downstream of this point is
            # provider-independent.
            if provider == PROVIDER_SOLD_COMPS:
                raw_results = data.get("items", [])
                parse = parse_comp_sold_comps
                is_non_usd = _is_non_usd_sold_comps_item
            else:
                raw_results = data.get("organic_results", [])
                parse = parse_comp
                is_non_usd = _is_non_usd_serpapi_result
            added = 0
            # BUI-678: counted separately from the ordinary "no title" / "bad
            # price" None-returns above, so a currency-specific rejection has
            # a distinct signal instead of vanishing into the same silent
            # `continue` as every other reason `parse()` can return None.
            non_usd_dropped = 0
            nonlocal non_usd_dropped_total
            for r in raw_results:
                comp = parse(r)
                if comp is None:
                    if is_non_usd(r):
                        non_usd_dropped += 1
                    continue
                if not comp["product_id"]:
                    continue
                if comp["product_id"] in seen_ids:
                    continue
                if hard_exclude(comp["title"]):
                    continue
                seen_ids.add(comp["product_id"])
                # BUI-657/KTD6: stamp provenance on the comp at the point it
                # joins the pool — provider/tier/query are already in scope
                # here and this is the one place downstream readers cannot
                # reconstruct them from (queries_used only records them at
                # book level, recoverable otherwise only by append order,
                # the exact positional-inference trap BUI-174/187 added
                # `_req_id` to escape). KTD7: `from_cache`/`observed_at` come
                # straight from the fetch that produced `data` — mtime on a
                # cache hit, this query's own fetch time otherwise — never
                # "now", so a re-run of a stable book doesn't restate old
                # comps as freshly observed.
                comp["provider"] = provider
                comp["tier"] = tier
                comp["query"] = nkw
                comp["from_cache"] = cache_hit
                comp["observed_at"] = response_fetched_at
                # BUI-524: only the inclusive tier passes route_slabs=True —
                # every other tier's behavior (add every non-excluded comp to
                # `comps`) is byte-for-byte unchanged. A slab comp is counted
                # toward `added` (queries_used stays an honest "how many new
                # things this query found" signal) but never joins the raw
                # `comps` pool.
                if route_slabs and _is_slab_comp(comp):
                    slab_comps.append(comp)
                    added += 1
                    continue
                comps.append(comp)
                added += 1
            non_usd_dropped_total += non_usd_dropped
            queries_used.append({
                "tier": tier,
                "nkw": nkw,
                "raw_results": len(raw_results),
                "new_comps": added,
                # BUI-678: how many of THIS query's raw_results the BUI-675
                # currency gate rejected — the gap between raw_results and
                # new_comps that isn't already explained by dedup/hard-excludes
                # is otherwise unnamed. 0 on every query for a book/response
                # this gate never touched (the common case).
                "non_usd_dropped": non_usd_dropped,
                "cached": cache_hit,
                # SerpApi-only field; "" for a sold-comps.com response (its
                # data has no search_metadata).
                "ebay_url": data.get("search_metadata", {}).get("ebay_url", ""),
                "page": page,
                "outcome": "hit" if cache_hit else "live",
                "provider": provider,
            })
            # _has_next_page fails closed on a sold-comps.com response (no
            # serpapi_pagination object) — the BUI-523 page-2 gate stays
            # SerpApi-only without a special case here.
            return {"added": added, "has_next_page": _has_next_page(data)}

        # Tier 1 — base
        base_nkw = build_query(title, issue, year=year, publisher=publisher,
                               variant=variant, exclude_graded=exclude_graded)
        base_result = _run("base", base_nkw)

        # BUI-523: gated page-2 fetch of the SAME base query — see the
        # fetch_book_comps docstring for the full spend-gate rationale. Placed
        # here (before tiers 2/3) so any comps it adds are already in `comps`
        # when tiers 2/3 recompute their own thin/grade-tagged counts below.
        grade_tagged_after_base = sum(1 for c in comps if c["grade"] is not None)
        if base_result["has_next_page"] and grade_tagged_after_base < GRADE_TAGGED_THRESHOLD:
            _run("base", base_nkw, page=2)

        # Tier 2 — auto-broaden if thin. BUI-350: pass the real `vintage_year`
        # (even though the query text drops `year`) so a rebootable-masthead
        # vintage key's broadened query keeps the BUI-347 exclusion terms —
        # this applies to the CGC-proxy graded pass (`include_graded=True`)
        # just as much as the ordinary raw pass, since both share this same
        # tier.
        if len(comps) < THIN_RESULTS_THRESHOLD and year:
            broader_nkw = build_query(title, issue, year=None, publisher=publisher,
                                      variant=variant, exclude_graded=exclude_graded,
                                      vintage_year=year)
            _run("broader", broader_nkw)

        # BUI-565: `year` is int|None by here (coerced at the top of this try),
        # so this gate reads a string-year vintage book correctly too. Hoisted
        # above the BUI-581 tier, which shares it; tier 4 below reads the same
        # name rather than recomputing it.
        is_vintage = year is not None and year < _VINTAGE_YEAR_CUTOFF

        # Tier 2.5 — alternate masthead (BUI-581). A vintage book whose series
        # was RENAMED mid-run is listed under whichever masthead the issue
        # actually carried, so querying the other one collapses the pool toward
        # zero and the book gets priced as illiquid. Probe the counterpart name
        # once and keep whichever pool is DEEPER — no rename-issue-number is
        # asserted anywhere (see `_MASTHEAD_RENAME_PAIRS` for why that matters).
        #
        # Placed here, before tiers 3/4, so the rest of the ladder deepens the
        # masthead that actually has comps instead of spending its queries on
        # the dead one. Gated on the same thinness threshold as tier 2, so a
        # book whose base query already found a real pool costs zero extra
        # queries and its `comps` are byte-for-byte pre-BUI-581.
        #
        # A year-less book is deliberately NOT probed: without a year this
        # cannot tell a vintage issue (where both names denote one run) from a
        # modern relaunch (where "X-Men #1" and "Uncanny X-Men #1" are different
        # books), and swapping there would price the wrong comic. Not probing
        # leaves today's behavior exactly as it was.
        alt_title = _alias_masthead_title(title) if is_vintage else None
        if alt_title and len(comps) < THIN_RESULTS_THRESHOLD:
            primary_comps, primary_seen = comps, seen_ids
            # Give the probe its own accumulator so its depth is measured on its
            # own merits rather than being dedup-suppressed by the pool it is
            # competing against. `_run` resolves `comps`/`seen_ids` from this
            # scope on every call (it only mutates them, never rebinds), so
            # rebinding them here redirects it and restoring them undoes it.
            comps = []
            seen_ids = {self_id} if self_id else set()
            # `year=year` is LOAD-BEARING, not cosmetic — do not drop it the way
            # tier 2 drops it to broaden. Both names in a rename pair are also
            # rebootable mastheads, so without the year an alias query can win
            # the depth comparison on the OTHER volume's same-numbered issue and
            # price the wrong book: X-Men Vol.2 #1 (1991, a common $10 book)
            # would probe as "Uncanny X-Men 1" and, unyeared, could pull the
            # 1963 key's comps into its pool — a four-figure over-bid. With the
            # year in the query the alias probe for that book returns ~nothing
            # and correctly loses. Year is the only thing that separates two
            # volumes sharing an issue number (see `_publisher_qualifier`'s
            # Marvel note), which is also why the tier is gated on having one.
            alt_nkw = build_query(alt_title, issue, year=year,
                                  publisher=publisher, variant=variant,
                                  exclude_graded=exclude_graded)
            _run("alt-masthead", alt_nkw)
            if len(comps) > len(primary_comps):
                # Swap, never MERGE: each pool is internally consistent about
                # which masthead it searched, and blending them would quietly
                # mix in same-numbered issues of the other volume.
                masthead_swapped_to = alt_title
                title = alt_title
            else:
                comps, seen_ids = primary_comps, primary_seen

        # Tier 3 — grade-targeted if too few grade-tagged comps in pool so far
        target_grade = book.get("grade")
        if isinstance(target_grade, str):
            target_grade = parse_grade(target_grade)
        grade_tagged = sum(1 for c in comps if c["grade"] is not None)
        if target_grade is not None and grade_tagged < GRADE_TAGGED_THRESHOLD:
            label = _grade_label_for_query(target_grade)
            if label:
                grade_nkw = build_query(title, issue, year=year, publisher=publisher,
                                        variant=variant, grade_label=label,
                                        exclude_graded=exclude_graded)
                _run("grade-targeted", grade_nkw)

        # Tier 4 — conditional inclusive pass (BUI-524). See the
        # fetch_book_comps docstring for the full rationale. Gated tightly
        # against the 250/month SerpApi quota: fires only for a vintage book
        # (own cover year, not the query-text year tiers 2/3 may have
        # dropped) whose raw pool is STILL thin after tiers 1-3, and only on
        # a normal (non-`include_graded`) call — an explicit graded-only pass
        # already runs every tier inclusive, so a 4th inclusive tier there
        # would be pure duplicate spend.
        # BUI-565: `year` is int|None by here (coerced at the top of this
        # try), so `is_vintage` (computed above tier 2.5) is True for a
        # string-year vintage book too — it silently never was before, quietly
        # disabling the BUI-524 tier for exactly the vintage books that need it
        # most.
        if exclude_graded and is_vintage and len(comps) < THIN_RESULTS_THRESHOLD:
            inclusive_nkw = build_query(title, issue, year=year, publisher=publisher,
                                        variant=variant, exclude_graded=False,
                                        vintage_year=year)
            _run("inclusive", inclusive_nkw, route_slabs=True)

        # Tier 5 — variant-drop retry (BUI-588). BUI-304 made `variant` a query
        # keyword, which is right for text sellers actually put in listing
        # titles ("Newsstand" narrows correctly, 32 comps → 13) and wrong for a
        # collector/catalog descriptor they never use ("White Logo 1st Print"
        # took 37 comps → 0). A dead term is carried through EVERY tier above,
        # so the widening ladder cannot recover from it; the book lands as
        # `comps=0` with no error, which reads downstream as "illiquid" rather
        # than "our query was impossible".
        #
        # Fires only on an EXACTLY empty pool, where there is no pool depth left
        # to trade away. Deliberately not on a merely thin one: a valid variant
        # term costs real depth even when it works (that Newsstand book's fmv
        # went 8 comps/MEDIUM-HIGH → 3 comps/LOW when the variant was applied),
        # so dropping it to chase depth would swap identity-correctness for
        # apparent confidence.
        #
        # The recovered comps price the BASE cover, not this variant — which is
        # a defensible floor for most variants and a wrong anchor for a scarce
        # few. That judgment is NOT made here: `variant_dropped` reports the
        # substitution to the caller (comic-fmv turns it into a needs-manual
        # `flag_reason`) instead of silently writing a variant-blind number.
        if variant and not comps:
            no_variant_nkw = build_query(title, issue, year=year,
                                         publisher=publisher, variant=None,
                                         exclude_graded=exclude_graded)
            _run("no-variant", no_variant_nkw)
            if comps:
                variant_dropped = variant
            # Still empty ⇒ the variant was not the cause; this is a genuine
            # no-comps book and must not be flagged as a variant problem.

        out_input = {
            "item_id": self_id or None,
            "title": input_title,
            "issue": issue,
            "year": year,
            "publisher": publisher,
            "grade": target_grade,
        }
        # BUI-174/187: echo back the caller's correlation id (when present) so
        # a batch driver can map results to inputs by identity, not list
        # position. A bare item_id is not reliable (may be absent or shared),
        # so the id is a dedicated field threaded by the caller; standalone
        # callers omit it.
        req_id = book.get("_req_id")
        if req_id is not None:
            out_input["_req_id"] = req_id
        return {
            "input": out_input,
            "queries_used": queries_used,
            "comps": comps,
            # BUI-524: always present (shape parity) — empty unless the
            # tier-4 inclusive pass fired and found genuine CGC/CBCS slab
            # comps.
            "slab_comps": slab_comps,
            # BUI-535: whether the batch-scoped breaker had tripped by the
            # time this book finished — surfaced so callers (comic-fmv, a
            # human skimming --out) can distinguish "outage" from "priced".
            "breaker_tripped": _breaker_tripped(),
            # BUI-581: the OTHER masthead these comps were actually found
            # under, or None when the caller's own title was queried. Not a
            # correction of `input.title` — a record that the pool describes
            # the same book under a different name.
            "masthead_swapped_to": masthead_swapped_to,
            # BUI-588: the variant term that had to be DROPPED before any comp
            # was found, or None. Non-None means this pool prices the base
            # cover, not that variant — the caller must surface the trade, not
            # bury it.
            "variant_dropped": variant_dropped,
            # BUI-678: total comps the BUI-675 currency gate rejected across
            # every tier this book ran — 0 for the overwhelming common case.
            # A caller compares this to `comps`/`comp_count_total` to tell a
            # currency-gate-emptied pool apart from a genuine no-comps book.
            "non_usd_dropped": non_usd_dropped_total,
        }
    except Exception as e:  # noqa: BLE001 — BUI-537: preserve the partial
        # trail rather than losing it; see the docstring above. `book.get(...)`
        # throughout (not the local `title`/`issue`/... names) because those
        # may never have been assigned if the exception fired before they
        # were (e.g. a missing "title"/"issue" key).
        out_input = {
            "item_id": (str(book.get("item_id")) if book.get("item_id") else None),
            "title": book.get("title"),
            "issue": (str(book.get("issue")) if book.get("issue") is not None else None),
            "year": book.get("year"),
            "publisher": book.get("publisher"),
            "grade": book.get("grade"),
        }
        req_id = book.get("_req_id")
        if req_id is not None:
            out_input["_req_id"] = req_id
        return {
            "input": out_input,
            "queries_used": queries_used,
            "comps": comps,
            "slab_comps": slab_comps,
            "breaker_tripped": _breaker_tripped(),
            "masthead_swapped_to": masthead_swapped_to,
            "variant_dropped": variant_dropped,
            "non_usd_dropped": non_usd_dropped_total,
            "error": str(e),
        }


def _grade_label_for_query(grade: float) -> str | None:
    """Pick a coarse letter grade to add to a query. Stays inside the bucket
    that contains `grade` so the search doesn't drift away from the target."""
    if grade >= 9.0:
        return "NM"
    if grade >= 8.0:
        return "VF"
    if grade >= 7.0:
        return "VF"  # FN/VF tagging is rare; VF surfaces upper bracket
    if grade >= 6.0:
        return "FN"
    if grade >= 4.5:
        return "VG"
    if grade >= 3.0:
        return "GD"
    return None


# ─── Batch driver ─────────────────────────────────────────────────────────────

def run_batch(books: list[dict], api_key: str, *, force: bool = False,
              ttl_sec: int = DEFAULT_CACHE_TTL_SEC,
              max_workers: int = DEFAULT_MAX_WORKERS) -> list[dict]:
    """Fan out across books with a thread pool.

    BUI-535: one `_CircuitBreaker` is created here and threaded explicitly
    into every `fetch_book_comps()` call for this batch — scoped to this one
    `run_batch()` call, not a module-level singleton, so a fresh invocation
    (e.g. a re-run after "SerpApi appears down") always starts with a clean,
    untripped breaker. `force` does not exempt a book from it (see fetch()).

    BUI-545: the secondary provider's config resolves ONCE per batch here —
    provider order from the env, key from load_sold_comps_key(), and its own
    breaker (same threshold, terminal-failures-only accounting; see
    fetch_sold_comps). No key → no failover tier (pre-BUI-545 behavior), with
    a one-line stderr nudge so the degradation is visible.
    """
    results: list[dict] = [None] * len(books)
    breaker = _CircuitBreaker(CIRCUIT_BREAKER_THRESHOLD, total_books=len(books))
    providers = _provider_order()
    sold_comps_key = (load_sold_comps_key()
                      if PROVIDER_SOLD_COMPS in providers else None)
    sold_comps_breaker = None
    if sold_comps_key:
        sold_comps_breaker = _CircuitBreaker(
            CIRCUIT_BREAKER_THRESHOLD, total_books=len(books),
            provider_name=PROVIDER_SOLD_COMPS,
        )
    elif PROVIDER_SOLD_COMPS in providers:
        print(
            "note: SOLD_COMPS_KEY not set — running without the "
            "sold-comps.com provider (BUI-545). SerpApi's sold engine has "
            "been login-walled since 2026-07-23, so expect fetch-errs until "
            "a key is configured (apps/ebay/.env).",
            file=sys.stderr,
        )
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_book_comps, b, api_key, force=force, ttl_sec=ttl_sec,
                       breaker=breaker, sold_comps_key=sold_comps_key,
                       sold_comps_breaker=sold_comps_breaker,
                       providers=providers): i
            for i, b in enumerate(books)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            breaker.book_completed()
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001  # batch boundary — capture per-book errors, continue
                # BUI-537: fetch_book_comps() now catches its own exceptions
                # internally and always returns a dict carrying whatever
                # partial queries_used/comps trail it had gathered (see its
                # docstring) — this branch should be effectively unreachable
                # in normal operation. Kept only as a last-resort guard for a
                # truly catastrophic failure that never even reached
                # fetch_book_comps's own try/except (e.g. the future itself
                # being cancelled) — there's no partial trail to recover here
                # since it lives inside that function's local scope.
                book = books[i]
                results[i] = {
                    "input": book,
                    "queries_used": [],
                    "comps": [],
                    "breaker_tripped": bool(breaker.tripped) or bool(
                        sold_comps_breaker is not None
                        and sold_comps_breaker.tripped),
                    "error": str(e),
                }
    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _print_human(results: list[dict]) -> None:
    for r in results:
        inp = r["input"]
        label = f"{inp['title']} #{inp['issue']}"
        if inp.get("year"):
            label += f" ({inp['year']})"
        if "error" in r:
            print(f"  {label}: ERROR {r['error']}")
            continue
        n_total = len(r["comps"])
        n_graded = sum(1 for c in r["comps"] if c["grade"] is not None)
        # BUI-523: a page-2 entry shares its tier's name (e.g. "base") with
        # its own page-1 entry — tag it "(pN)" here so a human skimming
        # --quiet=false output can see the gated extra-page fetch fired,
        # without changing the stored "tier"/"page" fields other consumers
        # read. BUI-537 made `page` always present (including page 1) — only
        # tag when it's not the implicit default, or every entry would show
        # "(p1)". BUI-545: likewise tag entries the non-default provider
        # served (e.g. "base[serpapi]") so a failover is visible at a
        # glance; the default primary stays untagged to keep the common
        # case terse.
        def _tier_label(q: dict) -> str:
            label = q["tier"]
            if q.get("page", 1) != 1:
                label += f'(p{q["page"]})'
            prov = q.get("provider")
            if prov and prov != DEFAULT_PROVIDER_ORDER[0]:
                label += f"[{prov}]"
            return label

        tiers = ",".join(_tier_label(q) for q in r["queries_used"])
        cached = sum(1 for q in r["queries_used"] if q.get("cached"))
        breaker_note = " [breaker-tripped]" if r.get("breaker_tripped") else ""
        # BUI-678: name it inline on the book's own line — this is the count
        # that turns a bare "0 comps" into a currency-gate drop rather than a
        # genuine no-comps book.
        non_usd = r.get("non_usd_dropped") or 0
        non_usd_note = f" [non-USD dropped: {non_usd}]" if non_usd else ""
        print(f"  {label}: {n_total} comps ({n_graded} grade-tagged) "
              f"tiers=[{tiers}] cached={cached}/{len(r['queries_used'])}"
              f"{breaker_note}{non_usd_note}")

    # BUI-535: aggregate stdout visibility (distinct from the one-time stderr
    # warning _CircuitBreaker.record_error() prints the instant it trips) —
    # so a human skimming --quiet=false output sees the batch was affected
    # even if they missed the stderr line.
    n_breaker_tripped = sum(1 for r in results if r.get("breaker_tripped"))
    if n_breaker_tripped:
        # BUI-545: breaker_tripped is now the OR of both providers' breakers,
        # so this line names neither — the one-time stderr warning from
        # _CircuitBreaker.record_error() already said which provider died.
        print(
            f"\n  A provider circuit breaker tripped during this batch — "
            f"{n_breaker_tripped} book(s) affected (failover, cache-only, "
            "or fetch-err); re-run later."
        )

    # BUI-678: a currency-rejected comp (BUI-675's gate) is a SUCCESSFUL fetch
    # whose parse discarded every result — a distinct, silent shape from both
    # a fetch-err (queries_used carries 'error') and a genuine no-comps book
    # (queries_used is clean AND non-usd_dropped is 0). Unconditional stderr
    # line (not folded into the stdout summary above) so it can't be missed
    # under --quiet, matching this repo's other loud-diagnostic precedents.
    n_non_usd_dropped = sum(r.get("non_usd_dropped") or 0 for r in results)
    if n_non_usd_dropped:
        n_books_affected = sum(
            1 for r in results if r.get("non_usd_dropped"))
        print(
            f"note: the BUI-675 currency gate dropped {n_non_usd_dropped} "
            f"non-USD comp(s) across {n_books_affected} book(s) in this "
            "batch — a successful fetch whose parse rejected every result, "
            "NOT a fetch error and not necessarily a genuine no-comps book. "
            "See each book's `non_usd_dropped` count.",
            file=sys.stderr,
        )


def _read_batch(path: str) -> list[dict]:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text())


def _write_out(path: str | None, data) -> None:
    if path is None:
        return
    if path == "-":
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    Path(path).write_text(json.dumps(data, indent=2))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="sold-comps",
        description="Fetch eBay sold listings for a comic via SerpApi.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=_version_string(),
        help="Print the installed version and the git SHA/date it was built "
             "from, then exit. Use this to check for a stale `uv tool install` "
             "(see scripts/install.sh).",
    )
    p.add_argument("--batch", help="Path to JSON batch file ('-' for stdin).")
    p.add_argument("--title", help="Series title (single-query mode).")
    p.add_argument("--issue", help="Issue number (single-query mode).")
    p.add_argument("--year", type=int, help="Cover year (single-query mode).")
    p.add_argument("--publisher", help="Publisher (recommended for indie titles).")
    p.add_argument("--variant", help="Distribution variant keyword (e.g. Newsstand).")
    p.add_argument("--grade", type=float, help="Target grade (single-query mode).")
    p.add_argument("--item-id", help="Self-exclude this product_id from comps.")
    p.add_argument("--include-graded", action="store_true",
                   help="Include CGC/CBCS graded (slab) comps instead of "
                        "excluding them (BUI-348, for the CGC-proxy tier). "
                        "Default: graded copies are excluded.")
    p.add_argument("--out", help="Write full JSON to this path ('-' for stdout).")
    p.add_argument("--force", action="store_true",
                   help="Bypass cache and refetch.")
    p.add_argument("--cache-ttl-days", type=float, default=7.0,
                   help="Cache TTL in days (default: 7).")
    p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
                   help="Thread pool size for batch mode (default: 10).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress human summary on stdout.")
    args = p.parse_args(argv)

    api_key = load_serpapi_key()
    ttl_sec = int(args.cache_ttl_days * 24 * 3600)

    if args.batch:
        books = _read_batch(args.batch)
        if not isinstance(books, list):
            print("Error: batch file must contain a JSON array.", file=sys.stderr)
            return 2
    elif args.title and args.issue:
        books = [{
            "title": args.title,
            "issue": args.issue,
            "year": args.year,
            "publisher": args.publisher,
            "variant": args.variant,
            "grade": args.grade,
            "item_id": args.item_id,
            "include_graded": args.include_graded,
        }]
    else:
        p.error("provide --batch <file> or (--title and --issue)")

    results = run_batch(books, api_key, force=args.force, ttl_sec=ttl_sec,
                        max_workers=args.max_workers)

    if not args.quiet:
        _print_human(results)

    if args.out:
        _write_out(args.out, results)
    elif args.quiet:
        # Quiet + no --out is a misuse; emit JSON to stdout so callers get something.
        _write_out("-", results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
